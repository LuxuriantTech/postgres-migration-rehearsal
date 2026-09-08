import { createHash } from "node:crypto";
import { lstat, mkdir, readFile, realpath, stat, writeFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";

const projectRoot = resolve(import.meta.dirname, "..");
const sourceRoot = resolve(projectRoot, "web/control-room");
const assets = ["app.css", "app.js", "favicon.svg", "index.html", "site.webmanifest"];

function outputArgument(argv) {
  if (argv.length === 0) return resolve(projectRoot, "dist/control-room");
  if (argv.length === 2 && argv[0] === "--output") return resolve(argv[1]);
  throw new Error("usage: build_control_room.mjs [--output EMPTY_DIRECTORY]");
}

const outputRoot = outputArgument(process.argv.slice(2));
if (outputRoot === projectRoot || dirname(outputRoot) === "/") {
  throw new Error("refusing broad output directory");
}
try {
  await stat(outputRoot);
  throw new Error(`output must not exist: ${basename(outputRoot)}`);
} catch (error) {
  if (error?.code !== "ENOENT") throw error;
}

await mkdir(dirname(outputRoot), { recursive: true });
const outputParent = dirname(outputRoot);
const parentStat = await lstat(outputParent);
if (
  !parentStat.isDirectory()
  || parentStat.isSymbolicLink()
  || (await realpath(outputParent)) !== outputParent
) {
  throw new Error("output parent must be a real directory");
}
const sourceStat = await lstat(sourceRoot);
if (!sourceStat.isDirectory() || sourceStat.isSymbolicLink()) {
  throw new Error("control-room source root must be a real directory");
}
await mkdir(outputRoot, { recursive: false });
const manifest = [];
for (const name of assets) {
  const sourcePath = resolve(sourceRoot, name);
  const fileStat = await lstat(sourcePath);
  if (!fileStat.isFile() || fileStat.isSymbolicLink()) {
    throw new Error(`control-room source asset is not a regular file: ${name}`);
  }
  const bytes = await readFile(sourcePath);
  await writeFile(resolve(outputRoot, name), bytes, { flag: "wx", mode: 0o644 });
  manifest.push({
    path: name,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    size: bytes.length
  });
}
const manifestBytes = `${JSON.stringify({ schema_version: 1, files: manifest }, null, 2)}\n`;
await writeFile(resolve(outputRoot, "asset-manifest.json"), manifestBytes, {
  flag: "wx",
  mode: 0o644
});
process.stdout.write(`control-room build: ${outputRoot}\n`);
