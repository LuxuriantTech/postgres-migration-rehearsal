import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { lstat, readFile, readdir, realpath } from "node:fs/promises";
import { dirname, resolve } from "node:path";

const projectRoot = resolve(import.meta.dirname, "..");
const sourceRoot = resolve(projectRoot, "web/control-room");
const buildScript = resolve(projectRoot, "scripts/build_control_room.mjs");
const assets = ["app.css", "app.js", "favicon.svg", "index.html", "site.webmanifest"];
const bundleFiles = [...assets, "asset-manifest.json"].sort();

function outputArgument(argv) {
  if (argv.length === 0) return resolve(projectRoot, "dist/control-room");
  if (argv.length === 2 && argv[0] === "--output") return resolve(argv[1]);
  throw new Error("usage: prepare_control_room.mjs [--output DIRECTORY]");
}

async function expectedBundle() {
  const sourceStat = await lstat(sourceRoot);
  if (!sourceStat.isDirectory() || sourceStat.isSymbolicLink()) {
    throw new Error("control-room source root must be a real directory");
  }
  const expected = new Map();
  const manifest = [];
  for (const name of assets) {
    const path = resolve(sourceRoot, name);
    const fileStat = await lstat(path);
    if (!fileStat.isFile() || fileStat.isSymbolicLink()) {
      throw new Error(`control-room source asset is not a regular file: ${name}`);
    }
    const bytes = await readFile(path);
    expected.set(name, bytes);
    manifest.push({
      path: name,
      sha256: createHash("sha256").update(bytes).digest("hex"),
      size: bytes.length
    });
  }
  expected.set(
    "asset-manifest.json",
    Buffer.from(`${JSON.stringify({ schema_version: 1, files: manifest }, null, 2)}\n`)
  );
  return expected;
}

async function verifyBundle(outputRoot, expected) {
  const rootStat = await lstat(outputRoot);
  if (
    !rootStat.isDirectory()
    || rootStat.isSymbolicLink()
    || (await realpath(outputRoot)) !== outputRoot
  ) {
    throw new Error("control-room output must be a real directory");
  }
  const observedNames = (await readdir(outputRoot)).sort();
  if (JSON.stringify(observedNames) !== JSON.stringify(bundleFiles)) {
    throw new Error("control-room output does not match the source bundle");
  }
  for (const name of bundleFiles) {
    const path = resolve(outputRoot, name);
    const fileStat = await lstat(path);
    if (!fileStat.isFile() || fileStat.isSymbolicLink()) {
      throw new Error("control-room output does not match the source bundle");
    }
    const bytes = await readFile(path);
    if (!bytes.equals(expected.get(name))) {
      throw new Error("control-room output does not match the source bundle");
    }
  }
}

const outputRoot = outputArgument(process.argv.slice(2));
if (outputRoot === projectRoot || dirname(outputRoot) === "/") {
  throw new Error("refusing broad output directory");
}
const expected = await expectedBundle();
let created = false;
try {
  await lstat(outputRoot);
} catch (error) {
  if (error?.code !== "ENOENT") throw error;
  const built = spawnSync(process.execPath, [buildScript, "--output", outputRoot], {
    cwd: projectRoot,
    encoding: "utf8"
  });
  if (built.error) throw built.error;
  if (built.status !== 0) {
    throw new Error(built.stderr.trim() || "control-room build failed");
  }
  created = true;
}
await verifyBundle(outputRoot, expected);
process.stdout.write(`control-room bundle ${created ? "created" : "verified"}: ${outputRoot}\n`);
