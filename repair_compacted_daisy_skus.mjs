#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const repoRoot = path.dirname(fileURLToPath(import.meta.url));
const shopDir = path.join(repoRoot, "shops", "daisyflowdigital");
const workbookPath = path.join(shopDir, "Etsy_SEO_Generator.xlsx");
const lockPath = path.join(shopDir, ".product-folder-compaction.lock");

const hash = (buffer) => crypto.createHash("sha256").update(buffer).digest("hex");
const stamp = () => new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
const skuForFolder = (folder) => `dd_${folder.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase()}`;

async function snapshot(file) {
  const [bytes, stat] = await Promise.all([fs.readFile(file), fs.stat(file)]);
  return { sha256: hash(bytes), size: stat.size, mtimeMs: stat.mtimeMs };
}

const same = (a, b) => a.sha256 === b.sha256 && a.size === b.size && a.mtimeMs === b.mtimeMs;
const stable = (value) => value instanceof Date ? { type: "date", value: value.toISOString() } : (value ?? null);
const normalized = (sheet) => sheet.getRange(`A1:R${sheet.getUsedRange().rowCount}`).values
  .map((row) => Array.from({ length: 18 }, (_, column) => stable(row[column])));

const runId = stamp();
let lock;
let tempPath;
try {
  lock = await fs.open(lockPath, "wx", 0o600);
  await lock.writeFile(`${JSON.stringify({ pid: process.pid, runId, purpose: "repair-padded-skus" })}\n`);

  const source = await snapshot(workbookPath);
  const backupPath = path.join(shopDir, `Etsy_SEO_Generator.backup_compacted_sku_repair_${runId}.xlsx`);
  tempPath = path.join(shopDir, `.Etsy_SEO_Generator.compacted_sku_repair_${runId}.tmp.xlsx`);
  await fs.copyFile(workbookPath, backupPath, fs.constants.COPYFILE_EXCL);
  const backup = await snapshot(backupPath);
  if (source.sha256 !== backup.sha256 || source.size !== backup.size) throw new Error("Backup verification failed");

  const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(workbookPath));
  const sheet = workbook.worksheets.getItem("Listings");
  const before = normalized(sheet);
  const changes = [];
  for (let index = 3; index < before.length; index += 1) {
    const folder = before[index][1];
    if (typeof folder !== "string" || !/^product-[0-9]+$/.test(folder)) continue;
    const expected = skuForFolder(folder);
    if (before[index][17] === expected) continue;
    sheet.getRange(`R${index + 1}`).values = [[expected]];
    changes.push({ row: index + 1, folder, oldSku: before[index][17], newSku: expected });
  }

  const exported = await SpreadsheetFile.exportXlsx(workbook);
  await exported.save(tempPath);
  const staged = await SpreadsheetFile.importXlsx(await FileBlob.load(tempPath));
  const after = normalized(staged.worksheets.getItem("Listings"));
  if (before.length !== after.length) throw new Error("Workbook row count changed");
  for (let row = 0; row < before.length; row += 1) {
    for (let column = 0; column < 17; column += 1) {
      if (JSON.stringify(before[row][column]) !== JSON.stringify(after[row][column])) {
        throw new Error(`Non-SKU cell changed at row ${row + 1}, column ${column + 1}`);
      }
    }
  }
  for (let index = 3; index < after.length; index += 1) {
    const folder = after[index][1];
    if (typeof folder === "string" && /^product-[0-9]+$/.test(folder) && after[index][17] !== skuForFolder(folder)) {
      throw new Error(`Invalid final SKU at row ${index + 1}`);
    }
  }

  if (!same(source, await snapshot(workbookPath))) throw new Error("Concurrent workbook change detected; aborting");
  await fs.rename(tempPath, workbookPath);
  tempPath = null;
  console.log(JSON.stringify({ status: "complete", workbook: workbookPath, backup: backupPath, changes }, null, 2));
} finally {
  if (tempPath) {
    try { await fs.unlink(tempPath); } catch {}
  }
  if (lock) await lock.close();
  try { await fs.unlink(lockPath); } catch {}
}
