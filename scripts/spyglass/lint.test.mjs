// Tests for lint.mjs. Run with `npm test` (needs `npm ci` first).

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
	annotation,
	escapeData,
	escapeProperty,
	LintLogger,
	parseArgs,
	UsageError,
	undeclaredSymbolRules,
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
		]);
		assert.equal(args.sourceMap, "map.json");
		assert.equal(args.cacheDir, "cache");
		assert.equal(args.failOn, "warning");
		assert.equal(args.allowUnsupported, true);
		assert.equal(args.allowUndeclared, true);
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
