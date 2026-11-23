// Tests for lint.mjs. Run with `npm test` (needs `npm ci` first).

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
	annotation,
	escapeData,
	escapeProperty,
	LintLogger,
	parseArgs,
	registerSnapshotDensityFunctionTypes,
	registryCategory,
	UsageError,
	undeclaredSymbolRules,
	VanillaBaseline,
} from "./lint.mjs";

describe("parseArgs", () => {
	it("reads the required options and applies defaults", () => {
		assert.deepEqual(
			parseArgs(["--root", "pack", "--game-version", "1.21.11"]),
			{
				root: "pack",
				gameVersion: "1.21.11",
				sourceMap: undefined,
				cacheDir: ".spyglass-cache",
				failOn: "error",
				allowUnsupported: false,
				allowUndeclared: false,
				vanillaBaseline: true,
			},
		);
	});

	it("reads every option", () => {
		const args = parseArgs([
			"--root=pack",
			"--game-version=26.1",
			"--source-map=map.json",
			"--cache-dir=cache",
			"--fail-on=warning",
			"--allow-unsupported",
			"--allow-undeclared",
			"--no-vanilla-baseline",
		]);
		assert.equal(args.sourceMap, "map.json");
		assert.equal(args.cacheDir, "cache");
		assert.equal(args.failOn, "warning");
		assert.equal(args.allowUnsupported, true);
		assert.equal(args.allowUndeclared, true);
		assert.equal(args.vanillaBaseline, false);
	});

	it("rejects bad usage", () => {
		const bad = [
			[],
			["--root", "pack"],
			["--game-version", "1"],
			["--root", "p", "--game-version", "1", "--fail-on", "info"],
			["--root", "p", "--game-version", "1", "--nope"],
			["--root"],
			["--root", "p", "--game-version", "1", "stray"],
		];
		for (const argv of bad) {
			assert.throws(() => parseArgs(argv), UsageError, argv.join(" "));
		}
	});
});

describe("escaping", () => {
	it("escapes message data", () => {
		assert.equal(escapeData("50%\r\nnext"), "50%25%0D%0Anext");
	});

	it("escapes properties too", () => {
		assert.equal(escapeProperty("a:b,c"), "a%3Ab%2Cc");
	});
});

describe("annotation", () => {
	it("formats one without a location", () => {
		assert.equal(
			annotation("warning", "Skipped: 1.21", "line1\nline2"),
			"::warning title=Skipped%3A 1.21::line1%0Aline2",
		);
	});

	it("formats one with a location", () => {
		const text = annotation("error", "Spyglass (1.21)", "bad", {
			file: "data/a,b.mcfunction",
			line: 1,
			col: 2,
			endLine: 3,
			endCol: 4,
		});
		assert.equal(
			text,
			"::error file=data/a%2Cb.mcfunction,line=1,col=2,endLine=3,endColumn=4,title=Spyglass (1.21)::bad",
		);
	});

	it("formats a notice", () => {
		assert.equal(
			annotation("notice", "NOTE: x", "m"),
			"::notice title=NOTE%3A x::m",
		);
	});
});

describe("LintLogger", () => {
	it("records errors as problems", () => {
		const logger = new LintLogger("1.21");
		const original = console.error;
		console.error = () => {};
		try {
			logger.error("Bad dependency %s\nmore", "x");
		} finally {
			console.error = original;
		}
		assert.deepEqual(logger.problems, ["Bad dependency x"]);
	});

	it("notes the version Spyglass resolved", () => {
		const logger = new LintLogger("1.21");
		logger.info(
			'[resolveConfiguredVersion] Using config "1.21", selecting version 1.21',
		);
		assert.equal(logger.resolvedVersion, "1.21");
	});

	it("notes a version Spyglass does not know", () => {
		const logger = new LintLogger("99.9");
		logger.info("Could not find config version 99.9");
		assert.equal(logger.unsupported.length, 1);
		assert.match(logger.unsupported[0], /99\.9/);
	});

	it("works when its methods are detached", () => {
		const { info } = new LintLogger("1");
		assert.doesNotThrow(() => info("anything"));
	});
});

describe("registerSnapshotDensityFunctionTypes", () => {
	it("adds only the two missing registry IDs for the affected snapshots", () => {
		let registration;
		registerSnapshotDensityFunctionTypes({
			config: { env: { gameVersion: "26.3-snapshot-2" } },
			meta: {
				registerSymbolRegistrar: (name, value) => {
					registration = { name, ...value };
				},
			},
		});
		assert.equal(registration.name, "snapshot-density-function-types");
		const added = [];
		registration.registrar({
			query: (uri, category, id) => ({
				symbol: undefined,
				enter: (value) => added.push({ uri, category, id, value }),
			}),
		});
		assert.deepEqual(
			added.map(({ uri, category, id }) => ({ uri, category, id })),
			[
				{
					uri: "mcmeta://summary/registries.json",
					category: "worldgen/density_function_type",
					id: "minecraft:lerp",
				},
				{
					uri: "mcmeta://summary/registries.json",
					category: "worldgen/density_function_type",
					id: "minecraft:gradient",
				},
			],
		);
	});

	it("does not add symbols for other versions", () => {
		let registered = false;
		registerSnapshotDensityFunctionTypes({
			config: { env: { gameVersion: "26.3-snapshot-4" } },
			meta: {
				registerSymbolRegistrar: () => {
					registered = true;
				},
			},
		});
		assert.equal(registered, false);
	});

	it("does not duplicate registry symbols already present", () => {
		let registration;
		registerSnapshotDensityFunctionTypes({
			config: { env: { gameVersion: "26.3-snapshot-1" } },
			meta: {
				registerSymbolRegistrar: (name, value) => {
					registration = { name, ...value };
				},
			},
		});
		let additions = 0;
		registration.registrar({
			query: () => ({
				symbol: {},
				enter: () => {
					additions++;
				},
			}),
		});
		assert.equal(additions, 0);
	});
});

describe("undeclaredSymbolRules", () => {
	const reports = (rules) => rules.map((rule) => rule.then?.report);

	it("promotes reports to errors by default", () => {
		for (const report of reports(undeclaredSymbolRules(false))) {
			assert.ok(report === undefined || report === "error");
		}
	});

	it("keeps Spyglass's defaults when allowed", () => {
		assert.ok(reports(undeclaredSymbolRules(true)).includes("warning"));
	});
});

describe("registryCategory", () => {
	it("is the first folder inside the namespace", () => {
		assert.equal(
			registryCategory("data/minecraft/dimension_type/a.json"),
			"dimension_type",
		);
		assert.equal(
			registryCategory("data/x/worldgen/feature/sub/a.json"),
			"worldgen",
		);
		assert.equal(registryCategory("data/x/tags/block/a.json"), "tags");
	});

	it("has none outside data/<namespace>/<folder>/", () => {
		assert.equal(registryCategory("pack.mcmeta"), undefined);
		assert.equal(registryCategory("data/x/a.json"), undefined);
		assert.equal(registryCategory("assets/x/a/b.json"), undefined);
	});
});

describe("VanillaBaseline", () => {
	const unknown = "Unknown key \u201cdirt_provider\u201d";
	const vanilla = "data/minecraft/worldgen/configured_feature/oak.json";

	it("matches a warning Mojang triggers anywhere in the same folder", () => {
		const baseline = new VanillaBaseline();
		baseline.addWarning(vanilla, unknown);
		assert.equal(baseline.matches(vanilla, unknown), true);
		assert.equal(
			baseline.matches("data/mine/worldgen/biome/x.json", unknown),
			true,
		);
	});

	it("never matches a different message or folder", () => {
		const baseline = new VanillaBaseline();
		baseline.addWarning(vanilla, unknown);
		assert.equal(
			baseline.matches(vanilla, "Unknown key \u201ctypo\u201d"),
			false,
		);
		assert.equal(
			baseline.matches("data/mine/loot_table/x.json", unknown),
			false,
		);
		assert.equal(baseline.matches("pack.mcmeta", unknown), false);
		assert.equal(new VanillaBaseline().matches(vanilla, unknown), false);
	});
});
