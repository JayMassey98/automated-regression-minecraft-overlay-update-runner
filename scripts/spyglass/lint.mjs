#!/usr/bin/env node

// Headless Spyglass lint runner.
//
// Spyglass has no official command-line linter, so this drives the same
// `Service` that the Spyglass language server uses, analyses every file in a
// pack directory and reports the diagnostics.
//
// Usage:
//   node lint.mjs --root <pack dir> --game-version <id> [options]
//
// Options:
//   --source-map <file>        JSON {flattened path: repo path} so annotations
//                              point at real files.
//   --cache-dir <dir>          where Spyglass caches downloaded vanilla data
//                              (default is ./.spyglass-cache).
//   --fail-on error|warning    lowest severity that fails the run (default:
//                              error).
//   --allow-unsupported        exit 0 with a warning when Spyglass cannot lint
//                              this version (for snapshots that may have just
//                              released, so Spyglass may not support it yet).
//   --allow-undeclared         keep Spyglass's default of only warning about
//                              unknown vanilla blocks/items/tags/etc. By
//                              default these are errors, because a block that
//                              doesn't exist in a version means the pack won't
//                              work there.
//   --no-vanilla-baseline      report every warning as a warning. By default a
//                              warning that Mojang's own data for the same
//                              Minecraft version also triggers is reported as
//                              a NOTE instead (see "Snapshot schema lag"
//                              below).
//
// Exit codes: 0 = clean, 1 = lint problems or bad arguments,
// 2 = Spyglass could not lint it.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { format, parseArgs as parseNodeArgs } from "node:util";
import * as core from "@spyglassmc/core";
import { getNodeJsExternals } from "@spyglassmc/core/lib/nodejs.js";
import * as je from "@spyglassmc/java-edition";
import * as locales from "@spyglassmc/locales";
import * as mcdoc from "@spyglassmc/mcdoc";

export const EXIT_LINT = 1;
export const EXIT_UNSUPPORTED = 2;

const FAIL_ON_LEVELS = ["error", "warning"];

// Spyglass reports which Minecraft version it resolved through its logger.
const RESOLVED_VERSION_PATTERN =
	/\[resolveConfiguredVersion\] Using config "(.+)", selecting version (\S+)/;
const UNKNOWN_VERSION_PATTERN =
	/Could not find config version|Could not determine release version/;
// The only Spyglass log lines worth showing in the CI log.
const SHOWN_INFO_PATTERN =
	/\[resolveConfiguredVersion\]|\[je-initialize\]|\[Project#(analyzeProject|getTrackedFiles)\]/;

/** A mistake in how the script was called (not a lint problem). */
export class UsageError extends Error {}

/**
 * Read the command line.
 * @param {string[]} argv arguments after the script name
 * @returns {{root: string, gameVersion: string, sourceMap?: string,
 *   cacheDir: string, failOn: "error" | "warning", allowUnsupported: boolean,
 *   allowUndeclared: boolean, vanillaBaseline: boolean}}
 */
export function parseArgs(argv) {
	let values;
	try {
		({ values } = parseNodeArgs({
			args: argv,
			allowPositionals: false,
			options: {
				root: { type: "string" },
				"game-version": { type: "string" },
				"source-map": { type: "string" },
				"cache-dir": { type: "string", default: ".spyglass-cache" },
				"fail-on": { type: "string", default: "error" },
				"allow-unsupported": { type: "boolean", default: false },
				"allow-undeclared": { type: "boolean", default: false },
				"no-vanilla-baseline": { type: "boolean", default: false },
			},
		}));
	} catch (error) {
		throw new UsageError(error.message);
	}
	if (!values.root || !values["game-version"]) {
		throw new UsageError("--root and --game-version are required");
	}
	if (!FAIL_ON_LEVELS.includes(values["fail-on"])) {
		throw new UsageError("--fail-on must be error or warning");
	}
	return {
		root: values.root,
		gameVersion: values["game-version"],
		sourceMap: values["source-map"],
		cacheDir: values["cache-dir"],
		failOn: values["fail-on"],
		allowUnsupported: values["allow-unsupported"],
		allowUndeclared: values["allow-undeclared"],
		vanillaBaseline: !values["no-vanilla-baseline"],
	};
}

// GitHub workflow commands need these characters escaped.
export const escapeData = (text) =>
	text.replace(/%/g, "%25").replace(/\r/g, "%0D").replace(/\n/g, "%0A");
export const escapeProperty = (text) =>
	escapeData(text).replace(/:/g, "%3A").replace(/,/g, "%2C");

/**
 * Build a GitHub Actions annotation line.
 * @param {"error" | "warning" | "notice"} level
 * @param {string} title
 * @param {string} message
 * @param {{file: string, line: number, col: number, endLine: number,
 *   endCol: number}} [location] where in a file the annotation points
 */
export function annotation(level, title, message, location) {
	const properties = [];
	if (location) {
		properties.push(
			`file=${escapeProperty(location.file)}`,
			`line=${location.line}`,
			`col=${location.col}`,
			`endLine=${location.endLine}`,
			`endColumn=${location.endCol}`,
		);
	}
	properties.push(`title=${escapeProperty(title)}`);
	return `::${level} ${properties.join(",")}::${escapeData(message)}`;
}

function appendSummary(markdown) {
	if (process.env.GITHUB_STEP_SUMMARY) {
		fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, `${markdown}\n`);
	}
}

const describeError = (error) => error?.stack ?? String(error);

/**
 * Collects what Spyglass logs. Spyglass logs (rather than throws) many
 * failures, so this is how we find out that it could not lint what we asked.
 */
export class LintLogger {
	/** Something broke: always a failure, even for snapshots. */
	problems = [];
	/** Spyglass doesn't know this Minecraft version yet: skipped for snapshots. */
	unsupported = [];
	/** The version Spyglass actually selected. */
	resolvedVersion;

	#gameVersion;

	constructor(gameVersion) {
		this.#gameVersion = gameVersion;
	}

	// Arrow functions, so Spyglass can call these without `this`.
	error = (message, ...rest) => {
		const text = format(message, ...rest);
		console.error(`[spyglass error] ${text}`);
		// A logged error such as a vanilla dependency that could not be
		// downloaded means linting would be meaningless, so we did not get a
		// trustworthy result.
		this.problems.push(text.split("\n")[0]);
	};

	warn = (message, ...rest) => {
		console.error(`[spyglass warn] ${format(message, ...rest)}`);
	};

	info = (message, ...rest) => {
		const text = format(message, ...rest);
		const resolved = text.match(RESOLVED_VERSION_PATTERN);
		if (resolved) {
			this.resolvedVersion = resolved[2];
		}
		if (UNKNOWN_VERSION_PATTERN.test(text)) {
			this.unsupported.push(
				`Spyglass does not know Minecraft version "${this.#gameVersion}" yet (it would have fallen back to the latest snapshot).`,
			);
		}
		if (SHOWN_INFO_PATTERN.test(text)) {
			console.log(text);
		}
	};

	log = () => {};
}

/**
 * Spyglass only analyses files that a FileWatcher tells it about (the language
 * server supplies one through the editor). Without this, it tracks no project
 * files at all.
 */
class DirectoryFileWatcher extends core.EventDispatcher {
	#watchedFiles = new core.UriStore();
	#rootUri;
	#externals;
	#isIncluded;

	constructor(externals, rootUri, isIncluded) {
		super();
		this.#externals = externals;
		this.#rootUri = rootUri;
		this.#isIncluded = isIncluded;
	}

	get watchedFiles() {
		return this.#watchedFiles;
	}

	async ready() {
		try {
			for (const uri of await core.fileUtil.getAllFiles(
				this.#externals,
				this.#rootUri,
			)) {
				if (this.#isIncluded(uri)) {
					this.#watchedFiles.add(uri);
				}
			}
			this.emit("ready", undefined);
		} catch (error) {
			this.emit("error", error);
		}
	}

	async close() {}
}

/**
 * Spyglass's default `undeclaredSymbol` rule only *warns* about vanilla things
 * that don't exist in the selected version ("cannot find block minecraft:x").
 * For a data pack that means it will not work on that version, so promote
 * those reports to errors (everything else is unchanged).
 */
export function undeclaredSymbolRules(allowUndeclared) {
	return core.VanillaConfig.lint.undeclaredSymbol.map((rule) =>
		!allowUndeclared && rule.then?.report
			? // biome-ignore lint/suspicious/noThenProperty: Spyglass rule schema requires a `then` key
				{ ...rule, then: { ...rule.then, report: "error" } }
			: rule,
	);
}

/**
 * Spyglass's registry summary omitted the two density-function types that
 * Minecraft added in the first 26.3 snapshots. Add only those exact vanilla
 * IDs for those versions; keep undeclared-symbol checking strict everywhere.
 */
export function registerSnapshotDensityFunctionTypes(ctx) {
	const affectedVersions = new Set([
		"26.3-snapshot-1",
		"26.3-snapshot-2",
		"26.3-snapshot-3",
	]);
	if (!affectedVersions.has(ctx.config.env.gameVersion)) {
		return;
	}
	ctx.meta.registerSymbolRegistrar("snapshot-density-function-types", {
		checksum: "26.3-snapshot-1-3-v1",
		registrar: (symbols) => {
			for (const id of ["minecraft:lerp", "minecraft:gradient"]) {
				const query = symbols.query(
					"mcmeta://summary/registries.json",
					"worldgen/density_function_type",
					id,
				);
				if (!query.symbol) {
					query.enter({ usage: { type: "declaration" } });
				}
			}
		},
	});
}

/**
 * Start Spyglass on the pack and analyse it. Failures are recorded on the
 * logger instead of being thrown, so every one of them is reported together.
 * @returns {Promise<{analysis?: {analyzedFiles: number},
 *   diagnosticsByUri: Map<string, readonly object[]>}>}
 */
async function analysePack({ args, rootUri, cacheRoot, externals, logger }) {
	// Last diagnostics event per file wins (files are reported during READY
	// and again by analyzeProject).
	const diagnosticsByUri = new Map();
	let service;
	try {
		service = new core.Service({
			logger,
			profilers: core.ProfilerFactory.noop(),
			project: {
				// spyglass.json in the pack root overrides this, but it must not
				// set env.gameVersion.
				defaultConfig: core.ConfigService.merge(core.VanillaConfig, {
					env: { gameVersion: args.gameVersion },
					lint: {
						undeclaredSymbol: undeclaredSymbolRules(args.allowUndeclared),
					},
				}),
				cacheRoot,
				externals,
				initializers: [
					mcdoc.initialize,
					je.initialize,
					registerSnapshotDensityFunctionTypes,
				],
				projectRoots: [rootUri],
			},
		});
		service.project.on("documentErrored", ({ errors, uri }) => {
			if (!uri.startsWith("archive://")) {
				diagnosticsByUri.set(uri, errors);
			}
		});
		await service.project.init();
		await service.project.ready({
			projectRootsWatcher: new DirectoryFileWatcher(
				externals,
				rootUri,
				(uri) => !service.project.shouldExclude(uri),
			),
		});
	} catch (error) {
		logger.problems.push(`Spyglass failed to start: ${describeError(error)}`);
	}

	let analysis;
	if (logger.problems.length === 0) {
		try {
			analysis = await service.project.analyzeProject();
		} catch (error) {
			logger.problems.push(
				`Spyglass failed while analysing the pack: ${describeError(error)}`,
			);
		}
	}
	if (logger.problems.length === 0 && (analysis?.analyzedFiles ?? 0) === 0) {
		logger.problems.push(
			"Spyglass analysed 0 files, so nothing was actually checked.",
		);
	}
	if (
		logger.problems.length === 0 &&
		logger.unsupported.length === 0 &&
		logger.resolvedVersion?.toLowerCase() !== args.gameVersion.toLowerCase()
	) {
		logger.unsupported.push(
			`Requested "${args.gameVersion}" but Spyglass selected "${logger.resolvedVersion ?? "nothing"}".`,
		);
	}
	try {
		await service?.project.close();
	} catch {
		// Nothing useful to do if closing fails; the results are already in.
	}
	return { analysis, diagnosticsByUri };
}

// ---------------------------------------------------------------------------
// Snapshot schema lag
//
// Spyglass's schemas (vanilla-mcdoc) are versioned per *release*: a field that
// changes in <version> is marked `since="<version>"` / `until="<version>"`, so
// Spyglass checks <version>-snapshot-N against the final <version> format.
// Mojang changes formats between snapshots, so a pack that matches exactly
// what the game accepts in a given snapshot can still get "Unknown key"
// warnings. Mojang's own data for that snapshot gets the very same warnings.
//
// So we also lint the vanilla data of the version being linted (only the
// top-level folders, such as `worldgen`, where the pack has warnings) and
// report a pack warning as a NOTE, not a warning, when vanilla triggers the
// same one somewhere in that folder. Schemas share structures between the
// registries of a folder (a pack may inline in noise_settings what Mojang keeps
// in density_function), so the match is per folder. A warning vanilla does not
// trigger stays a warning, so real mistakes (a key Mojang removed, a typo) are
// still visible. `--no-vanilla-baseline` turns this off.
// ---------------------------------------------------------------------------

/**
 * The top-level folder of a pack file inside its namespace, such as `worldgen`
 * for `data/<ns>/worldgen/noise_settings/nether.json`.
 * @param {string} relativePath path inside the pack, with forward slashes
 * @returns {string | undefined} undefined when it is not under `data/<ns>/`
 */
export function registryCategory(relativePath) {
	const parts = relativePath.split("/");
	return parts[0] === "data" && parts.length >= 4 ? parts[2] : undefined;
}

/** Which warnings Mojang's own data for one Minecraft version triggers. */
export class VanillaBaseline {
	#byCategory = new Map();

	/** Record a warning Spyglass raised on a vanilla file. */
	addWarning(relativePath, message) {
		const category = registryCategory(relativePath);
		if (category === undefined) {
			return;
		}
		if (!this.#byCategory.has(category)) {
			this.#byCategory.set(category, new Set());
		}
		this.#byCategory.get(category).add(message);
	}

	/** Does vanilla trigger this warning in the same top-level folder? */
	matches(relativePath, message) {
		const category = registryCategory(relativePath);
		return (
			category !== undefined &&
			(this.#byCategory.get(category)?.has(message) ?? false)
		);
	}
}

/** The path of a file URI inside `rootDir`, or undefined if it is not in it. */
function relativeToRoot(uri, rootDir) {
	try {
		const relative = path
			.relative(rootDir, fileURLToPath(uri))
			.split(path.sep)
			.join("/");
		return relative.startsWith("..") ? undefined : relative;
	} catch {
		return undefined; // not a file URI
	}
}

/**
 * Lint Mojang's own data for `args.gameVersion` in the top-level folders where
 * the pack has warnings, and remember which warnings it triggers.
 * @param {string} rootDir the pack root (its spyglass.json is reused)
 * @param {string[]} warnedFiles pack files (relative to the pack root) that
 *   have warnings
 * @returns {Promise<VanillaBaseline>}
 */
async function buildVanillaBaseline({
	args,
	rootDir,
	warnedFiles,
	cacheRoot,
	externals,
	logger,
}) {
	const categories = new Set(warnedFiles.map(registryCategory));
	categories.delete(undefined);
	const dependency = await je.dependency.getVanillaDatapack(
		externals,
		logger,
		args.gameVersion,
	);
	const files = await externals.archive.decompressBall(dependency.data, {
		stripLevel: dependency.stripLevel ?? 0,
	});

	// A fixed directory keeps Spyglass's per-root cache stable between runs.
	const tempDir = path.join(
		os.tmpdir(),
		`armour-vanilla-baseline-${args.gameVersion}`,
	);
	fs.rmSync(tempDir, { recursive: true, force: true });
	fs.mkdirSync(tempDir, { recursive: true });
	try {
		// Lint it with the same Spyglass settings as the pack.
		const config = path.join(rootDir, "spyglass.json");
		if (fs.existsSync(config)) {
			fs.copyFileSync(config, path.join(tempDir, "spyglass.json"));
		}
		const baseline = new VanillaBaseline();
		for (const file of files) {
			const relative = file.path.replace(/\\/g, "/");
			const isPackMeta = relative === "pack.mcmeta";
			if (
				file.type !== "file" ||
				!(
					isPackMeta ||
					(relative.startsWith("data/minecraft/") &&
						categories.has(registryCategory(relative)))
				)
			) {
				continue;
			}
			const target = path.resolve(tempDir, relative);
			if (!target.startsWith(tempDir + path.sep)) {
				continue; // never write outside the temporary directory
			}
			fs.mkdirSync(path.dirname(target), { recursive: true });
			fs.writeFileSync(target, file.data);
		}

		const vanillaLogger = new LintLogger(args.gameVersion);
		const { diagnosticsByUri } = await analysePack({
			args,
			rootUri: core.fileUtil.ensureEndingSlash(
				pathToFileURL(tempDir).toString(),
			),
			cacheRoot,
			externals,
			logger: vanillaLogger,
		});
		const trouble = [...vanillaLogger.problems, ...vanillaLogger.unsupported];
		if (trouble.length > 0) {
			throw new Error(trouble[0]);
		}
		for (const [uri, errors] of diagnosticsByUri) {
			const relative = relativeToRoot(uri, tempDir);
			for (const error of errors) {
				if (
					relative !== undefined &&
					error.severity === core.ErrorSeverity.Warning
				) {
					baseline.addWarning(relative, error.message);
				}
			}
		}
		return baseline;
	} finally {
		fs.rmSync(tempDir, { recursive: true, force: true });
	}
}

/**
 * Print each diagnostic as an annotation and count them. Warnings that
 * `baseline` says Mojang's own data also triggers are printed as NOTEs.
 * @param {VanillaBaseline} [baseline]
 * @returns {{errorCount: number, warningCount: number, noteCount: number,
 *   failing: number}}
 */
function reportDiagnostics(
	diagnosticsByUri,
	{ args, rootDir, sourceMap, baseline },
) {
	const threshold =
		args.failOn === "warning"
			? core.ErrorSeverity.Warning
			: core.ErrorSeverity.Error;
	const counts = { errorCount: 0, warningCount: 0, noteCount: 0, failing: 0 };
	const items = [];
	const byUri = [...diagnosticsByUri].sort(([a], [b]) => a.localeCompare(b));
	for (const [uri, errors] of byUri) {
		const relative = relativeToRoot(uri, rootDir);
		if (relative === undefined) {
			continue;
		}
		const file = sourceMap[relative] ?? relative;
		for (const error of errors) {
			if (error.severity < core.ErrorSeverity.Warning) {
				continue; // hints / information
			}
			const isError = error.severity >= core.ErrorSeverity.Error;
			const isNote = !isError && baseline?.matches(relative, error.message);
			items.push({ file, error, isError, isNote });
		}
	}

	const noteCount = items.filter((item) => item.isNote).length;
	if (noteCount > 0) {
		const plural = noteCount === 1 ? "" : "s";
		console.log(
			`NOTE: ${noteCount} Spyglass warning${plural} below ${noteCount === 1 ? "is" : "are"} reported as NOTE${plural} instead of warning${plural}, ` +
				`because Mojang's own ${args.gameVersion} data triggers the same warning${plural}. ` +
				"Spyglass versions its schemas per release, not per snapshot, so for a snapshot it can expect a different format than the game accepts. " +
				"This is a Spyglass limitation, not a problem with the pack. " +
				"Warnings that Mojang's data does not trigger are still reported as warnings; " +
				"use --no-vanilla-baseline to report these as warnings too.",
		);
	}

	for (const { file, error, isError, isNote } of items) {
		if (isNote) {
			counts.noteCount++;
		} else if (isError) {
			counts.errorCount++;
		} else {
			counts.warningCount++;
		}
		if (!isNote && error.severity >= threshold) {
			counts.failing++;
		}
		// Annotations are hidden from the raw log, so print the place too.
		const line = error.posRange.start.line + 1;
		const col = error.posRange.start.character + 1;
		const location = {
			file,
			line,
			col,
			endLine: error.posRange.end.line + 1,
			endCol: error.posRange.end.character + 1,
		};
		if (isNote) {
			console.log(`NOTE: ${file}:${line}:${col} ${error.message}`);
			console.log(
				annotation(
					"notice",
					`NOTE: Spyglass (${args.gameVersion})`,
					`${error.message} (NOTE: Spyglass snapshot schema lag; Mojang's own ${args.gameVersion} data triggers it too)`,
					location,
				),
			);
			continue;
		}
		console.log(`${file}:${line}:${col} ${error.message}`);
		console.log(
			annotation(
				isError ? "error" : "warning",
				`Spyglass (${args.gameVersion})`,
				error.message,
				location,
			),
		);
	}
	return counts;
}

/** Pack files (relative to the pack root) that Spyglass warned about. */
function warnedFilesOf(diagnosticsByUri, rootDir) {
	const files = [];
	for (const [uri, errors] of diagnosticsByUri) {
		const relative = relativeToRoot(uri, rootDir);
		if (
			relative !== undefined &&
			errors.some((error) => error.severity === core.ErrorSeverity.Warning)
		) {
			files.push(relative);
		}
	}
	return files;
}

/**
 * Lint a pack.
 * @returns {Promise<number>} the process exit code
 */
export async function lint(args) {
	const rootDir = path.resolve(args.root);
	const sourceMap = args.sourceMap
		? JSON.parse(fs.readFileSync(args.sourceMap, "utf8"))
		: {};
	const ensureSlash = core.fileUtil.ensureEndingSlash;
	const rootUri = ensureSlash(pathToFileURL(rootDir).toString());
	const cacheRoot = ensureSlash(
		pathToFileURL(path.resolve(args.cacheDir)).toString(),
	);
	fs.mkdirSync(path.resolve(args.cacheDir), { recursive: true });

	try {
		await locales.loadLocale("en");
	} catch (error) {
		console.error("Could not load locale; messages may show as keys.", error);
	}

	const logger = new LintLogger(args.gameVersion);
	const externals = getNodeJsExternals({ cacheRoot, logger });
	const { analysis, diagnosticsByUri } = await analysePack({
		args,
		rootUri,
		cacheRoot,
		externals,
		logger,
	});

	// A version Spyglass doesn't know yet is expected for new snapshots: skip
	// it (if allowed).
	if (logger.unsupported.length > 0) {
		const message = `Spyglass could not lint ${args.gameVersion}: ${[...new Set(logger.unsupported)].join(" | ")}`;
		if (args.allowUnsupported) {
			console.log(
				annotation("warning", `Spyglass skipped ${args.gameVersion}`, message),
			);
			appendSummary(
				`⚪ \`${args.gameVersion}\`: **skipped** (Spyglass does not support this version yet)`,
			);
			return 0;
		}
		console.log(
			annotation("error", `Spyglass cannot lint ${args.gameVersion}`, message),
		);
		appendSummary(
			`🔴 \`${args.gameVersion}\`: Spyglass does not support this version`,
		);
		return EXIT_UNSUPPORTED;
	}

	// Anything else going wrong is a real failure for every version, so it can
	// never pass silently.
	if (logger.problems.length > 0) {
		const message = `Spyglass could not lint ${args.gameVersion}: ${[...new Set(logger.problems)].slice(0, 5).join(" | ")}`;
		console.log(
			annotation("error", `Spyglass failed on ${args.gameVersion}`, message),
		);
		appendSummary(`🔴 \`${args.gameVersion}\`: Spyglass failed (see log)`);
		return EXIT_UNSUPPORTED;
	}

	// See "Snapshot schema lag" above. If the baseline cannot be built, every
	// warning is simply reported as a warning (never fewer than before).
	let baseline;
	const warnedFiles = args.vanillaBaseline
		? warnedFilesOf(diagnosticsByUri, rootDir)
		: [];
	if (warnedFiles.length > 0) {
		try {
			baseline = await buildVanillaBaseline({
				args,
				rootDir,
				warnedFiles,
				cacheRoot,
				externals,
				logger,
			});
		} catch (error) {
			console.log(
				`NOTE: could not check Mojang's ${args.gameVersion} data, so every warning is reported as a warning: ${describeError(error).split("\n")[0]}`,
			);
		}
	}

	const { errorCount, warningCount, noteCount, failing } = reportDiagnostics(
		diagnosticsByUri,
		{ args, rootDir, sourceMap, baseline },
	);
	const files = analysis?.analyzedFiles ?? diagnosticsByUri.size;
	const totals = `${files} files, ${errorCount} errors, ${warningCount} warnings${noteCount > 0 ? `, ${noteCount} notes` : ""}`;
	console.log(`Spyglass ${args.gameVersion}: ${totals}`);
	appendSummary(
		`${failing > 0 ? "🔴" : "🟢"} \`${args.gameVersion}\`: ${totals}${noteCount > 0 ? " (notes: Spyglass snapshot schema lag, Mojang's own data triggers them too)" : ""}`,
	);
	return failing > 0 ? EXIT_LINT : 0;
}

async function main() {
	let args;
	try {
		args = parseArgs(process.argv.slice(2));
	} catch (error) {
		if (error instanceof UsageError) {
			console.error(`Error: ${error.message}`);
			return EXIT_LINT;
		}
		throw error;
	}
	return lint(args);
}

// Only run when started from the command line, so tests can import the helpers.
// Exit explicitly: Spyglass can leave handles open that would keep Node alive.
if (
	process.argv[1] &&
	import.meta.url === pathToFileURL(fs.realpathSync(process.argv[1])).href
) {
	const exitCode = await main();
	// process.exit() can cut off output that is still queued when stdout is a
	// pipe (as in CI), so wait until everything printed has been written.
	for (const stream of [process.stdout, process.stderr]) {
		await new Promise((resolve) => stream.write("", resolve));
	}
	process.exit(exitCode);
}
