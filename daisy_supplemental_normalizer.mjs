#!/usr/bin/env node
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const apply = process.argv.includes("--apply");
if (apply === process.argv.includes("--dry-run")) throw new Error("Choose exactly one of --dry-run or --apply");
const root = path.join(path.dirname(new URL(import.meta.url).pathname), "shops", "daisyflowdigital");
const canonicalRe = /^product-(\d+)$/;
const noncanonicalRe = /^product-(\d+)(?:\s+.+)$/;
const lockPath = path.join(root, ".supplemental-product-normalizer.lock");
const stamp = () => new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");

async function filesUnder(dir, rel = "") {
  let entries;
  try { entries = await fs.readdir(path.join(dir, rel), { withFileTypes: true }); }
  catch (error) { if (error.code === "ENOENT") return []; throw error; }
  const out = [];
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    const child = path.join(rel, entry.name);
    if (entry.isDirectory()) out.push(...await filesUnder(dir, child));
    else if (entry.isFile() && entry.name !== ".DS_Store") out.push(child);
  }
  return out;
}

async function inventory(name) {
  const dir = path.join(root, name);
  const records = [];
  for (const base of ["images", "files"]) for (const relativePath of await filesUnder(dir, base)) {
    const stat = await fs.stat(path.join(dir, relativePath));
    records.push({ name, relativePath, dev: String(stat.dev), ino: String(stat.ino), size: stat.size,
      blocks: stat.blocks ?? null, mtimeMs: stat.mtimeMs, datalessOrZeroBlock: stat.size > 0 && stat.blocks === 0 });
  }
  return records;
}

const identity = records => records.map(r => [r.relativePath, r.dev, r.ino, r.size, r.blocks, r.mtimeMs].join(":" )).sort();
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

async function plan() {
  const entries = await fs.readdir(root, { withFileTypes: true });
  const canonical = entries.filter(e => e.isDirectory() && canonicalRe.test(e.name))
    .map(e => ({ name: e.name, number: Number(canonicalRe.exec(e.name)[1]) }))
    .sort((a, b) => a.number - b.number || a.name.localeCompare(b.name));
  const expected = Array.from({ length: canonical.length }, (_, i) => `product-${String(i + 1).padStart(2, "0")}`);
  if (!same(canonical.map(x => x.name), expected)) throw new Error("Canonical folders are not a gapless sequence before supplemental normalization");
  const candidates = entries.filter(e => e.isDirectory() && noncanonicalRe.test(e.name))
    .map(e => ({ name: e.name, number: Number(noncanonicalRe.exec(e.name)[1]) }))
    .sort((a, b) => a.number - b.number || a.name.localeCompare(b.name));
  const inspected = [];
  for (const candidate of candidates) inspected.push({ ...candidate, files: await inventory(candidate.name) });
  const retained = inspected.filter(x => x.files.length > 0);
  const empty = inspected.filter(x => x.files.length === 0);
  const mappings = retained.map((x, i) => ({ oldFolder: x.name, newFolder: `product-${String(canonical.length + i + 1).padStart(2, "0")}`, files: x.files.length }));
  return { canonical, candidates, inspected, retained, empty, mappings };
}

async function rollback(moves) {
  const errors = [];
  for (const move of [...moves].reverse()) try { await fs.rename(move.to, move.from); } catch (e) { errors.push(e.message); }
  if (errors.length) throw new Error(`Rollback incomplete: ${errors.join("; ")}`);
}

async function verify(p, preInventory) {
  const entries = await fs.readdir(root, { withFileTypes: true });
  const canonical = entries.filter(e => e.isDirectory() && canonicalRe.test(e.name))
    .map(e => ({ name: e.name, number: Number(canonicalRe.exec(e.name)[1]) })).sort((a, b) => a.number - b.number);
  const expected = Array.from({ length: p.canonical.length + p.retained.length }, (_, i) => `product-${String(i + 1).padStart(2, "0")}`);
  if (!same(canonical.map(x => x.name), expected)) throw new Error("Final canonical sequence mismatch");
  const remaining = entries.filter(e => e.isDirectory() && noncanonicalRe.test(e.name)).map(e => e.name);
  if (remaining.length) throw new Error(`Remaining noncanonical product dirs: ${remaining.join(", ")}`);
  const post = [];
  for (const mapping of p.mappings) post.push(...await inventory(mapping.newFolder));
  if (!same(identity(preInventory), identity(post))) throw new Error("Asset metadata identities changed");
  for (const folder of canonical) if ((await inventory(folder.name)).length === 0) throw new Error(`Empty canonical folder: ${folder.name}`);
  return { canonical: canonical.length, files: post.length, dataless: post.filter(x => x.datalessOrZeroBlock).length };
}

const p = await plan();
if (!apply) {
  process.stdout.write(JSON.stringify({ mode: "dry-run", canonical: p.canonical.length, retained: p.retained.map(x => ({ name: x.name, files: x.files.length })), empty: p.empty.map(x => x.name), mappings: p.mappings }, null, 2) + "\n");
  process.exit(0);
}

const runId = stamp();
const quarantine = path.join(root, `.supplemental-product-quarantine-${runId}`);
const manifestPath = path.join(root, `supplemental-product-normalizer-${runId}-manifest.json`);
let lock; const moves = [];
try {
  lock = await fs.open(lockPath, "wx", 0o600);
  await fs.mkdir(quarantine);
  for (const item of p.empty) {
    const from = path.join(root, item.name), to = path.join(quarantine, item.name);
    await fs.rename(from, to); moves.push({ from, to });
  }
  for (const mapping of p.mappings) {
    const from = path.join(root, mapping.oldFolder), to = path.join(root, mapping.newFolder);
    await fs.rename(from, to); moves.push({ from, to });
  }
  const preInventory = p.retained.flatMap(x => x.files);
  const verified = await verify(p, preInventory);
  const manifest = { schemaVersion: 1, status: "complete", runId, createdAt: new Date().toISOString(), quarantine,
    catalogNaturalOrder: p.candidates.map(x => x.name), mappings: p.mappings, emptyQuarantined: p.empty.map(x => x.name),
    assetVerification: "metadata identity only; content hashes skipped for dataless safety", preInventory, verified };
  await fs.writeFile(manifestPath, JSON.stringify(manifest, null, 2) + "\n", { flag: "wx" });
  process.stdout.write(JSON.stringify({ status: "complete", manifest: manifestPath, quarantine, mappings: p.mappings, verified }, null, 2) + "\n");
} catch (error) {
  if (moves.length) await rollback(moves);
  try { await fs.rmdir(quarantine); } catch {}
  throw error;
} finally {
  if (lock) await lock.close();
  try { await fs.unlink(lockPath); } catch {}
}
