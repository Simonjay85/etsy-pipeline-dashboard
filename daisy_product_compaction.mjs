#!/usr/bin/env node

/**
 * Transactionally compact Daisy Flow Digital product folders and workbook refs.
 *
 * Run with the loader-provided @oai/artifact-tool available in node_modules:
 *   node daisy_product_compaction.mjs --dry-run
 *   node daisy_product_compaction.mjs --apply
 *
 * This script never contacts Etsy. In apply mode it creates a byte-for-byte
 * workbook backup before staging or mutating shop data, uses an exclusive lock,
 * two-phase folder renames, and rolls folders back if workbook replacement fails.
 */

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const SHOP_SLUG = "daisyflowdigital";
const PRODUCT_RE = /^product-(\d+)$/;
const WORKBOOK_NAME = "Etsy_SEO_Generator.xlsx";
const SHEET_NAME = "Listings";
const FOLDER_COL = 1; // B, zero based
const SKU_COL = 17; // R, zero based
const COLUMN_COUNT = 18;

const argv = new Set(process.argv.slice(2));
const applyMode = argv.has("--apply");
const dryRunMode = argv.has("--dry-run");
if (applyMode === dryRunMode) {
  throw new Error("Choose exactly one of --dry-run or --apply");
}

const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname));
const shopDir = path.join(repoRoot, "shops", SHOP_SLUG);
const workbookPath = path.join(shopDir, WORKBOOK_NAME);
const lockPath = path.join(shopDir, ".product-folder-compaction.lock");

function timestamp() {
  return new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

function sha256Buffer(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

async function fileSnapshot(filePath) {
  const [buffer, stat] = await Promise.all([fs.readFile(filePath), fs.stat(filePath)]);
  return {
    sha256: sha256Buffer(buffer),
    size: stat.size,
    mtimeMs: stat.mtimeMs,
    mtimeIso: stat.mtime.toISOString(),
  };
}

function sameSnapshot(left, right) {
  return left.sha256 === right.sha256 && left.size === right.size && left.mtimeMs === right.mtimeMs;
}

function canonicalProductName(number) {
  return `product-${String(number).padStart(2, "0")}`;
}

function skuForFolder(folderName) {
  return `dd_${folderName.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase()}`;
}

function productNumber(name) {
  const match = PRODUCT_RE.exec(name);
  return match ? Number(match[1]) : null;
}

async function listCanonicalProductDirs() {
  const entries = await fs.readdir(shopDir, { withFileTypes: true });
  return entries
    .filter((entry) => entry.isDirectory() && PRODUCT_RE.test(entry.name))
    .map((entry) => ({ name: entry.name, number: productNumber(entry.name) }))
    .sort((a, b) => a.number - b.number || a.name.localeCompare(b.name));
}

async function regularFilesRecursively(rootDir, relative = "") {
  const current = path.join(rootDir, relative);
  let entries;
  try {
    entries = await fs.readdir(current, { withFileTypes: true });
  } catch (error) {
    if (error.code === "ENOENT") return [];
    throw error;
  }
  const files = [];
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    const childRelative = path.join(relative, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await regularFilesRecursively(rootDir, childRelative)));
    } else if (entry.isFile() && entry.name !== ".DS_Store") {
      files.push(childRelative);
    }
  }
  return files;
}

async function assetFiles(productDir) {
  const result = [];
  for (const assetSubdir of ["images", "files"]) {
    const nested = await regularFilesRecursively(productDir, assetSubdir);
    result.push(...nested);
  }
  return result.sort();
}

async function inventoryProductFiles(folderNames) {
  const records = [];
  for (const folder of folderNames) {
    const root = path.join(shopDir, folder);
    const relatives = await assetFiles(root);
    for (const relativePath of relatives) {
      const absolutePath = path.join(root, relativePath);
      // Metadata only: reading an APFS/iCloud dataless placeholder would hydrate it.
      const stat = await fs.stat(absolutePath);
      records.push({
        folder,
        relativePath,
        dev: String(stat.dev),
        ino: String(stat.ino),
        size: stat.size,
        blocks: stat.blocks ?? null,
        mtimeMs: stat.mtimeMs,
        datalessOrZeroBlock: stat.size > 0 && stat.blocks === 0,
      });
    }
  }
  return records;
}

function identityMultiset(records) {
  return records.map((record) => [
    record.relativePath,
    record.dev,
    record.ino,
    record.size,
    record.blocks,
    record.mtimeMs,
  ].join(":" )).sort();
}

function stableCell(value) {
  if (value instanceof Date) return { type: "date", value: value.toISOString() };
  if (value === undefined) return null;
  return value;
}

function valuesMatrix(sheet) {
  const used = sheet.getUsedRange();
  const raw = used?.values ?? [];
  return raw.map((row) => Array.from({ length: COLUMN_COUNT }, (_, column) => stableCell(row[column] ?? null)));
}

function formulasMatrix(sheet, rowCount) {
  if (rowCount === 0) return [];
  const raw = sheet.getRange(`A1:R${rowCount}`).formulas ?? [];
  return raw.map((row) => Array.from({ length: COLUMN_COUNT }, (_, column) => row[column] ?? null));
}

function etsyUrlMultiset(matrix) {
  const urls = [];
  for (const row of matrix) {
    for (const value of row) {
      if (typeof value !== "string") continue;
      const matches = value.match(/https?:\/\/[^\s"'<>]*etsy\.com[^\s"'<>]*/gi) ?? [];
      urls.push(...matches);
    }
  }
  return urls.sort();
}

function nonTargetCells(matrix) {
  return matrix.map((row) => row.filter((_, column) => column !== FOLDER_COL && column !== SKU_COL));
}

function matrixEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

async function importWorkbook(filePath) {
  const input = await FileBlob.load(filePath);
  return SpreadsheetFile.importXlsx(input);
}

async function renderAllSheets(workbook) {
  const rendered = [];
  const sheets = workbook.worksheets.items ?? [];
  if (sheets.length === 0) throw new Error("Workbook contains no worksheets");
  for (const sheet of sheets) {
    const preview = await workbook.render({ sheetName: sheet.name, autoCrop: "all", scale: 1, format: "png" });
    const bytes = new Uint8Array(await preview.arrayBuffer());
    if (bytes.length === 0) throw new Error(`Blank render for worksheet ${sheet.name}`);
    rendered.push({ sheet: sheet.name, pngBytes: bytes.length });
  }
  return rendered;
}

async function inspectWorkbook(workbook) {
  const sheet = workbook.worksheets.getItem(SHEET_NAME);
  const values = valuesMatrix(sheet);
  const formulas = formulasMatrix(sheet, values.length);
  return {
    sheet,
    values,
    formulas,
    etsyUrls: etsyUrlMultiset(values),
    nonTargetValues: nonTargetCells(values),
    nonTargetFormulas: nonTargetCells(formulas),
  };
}

async function buildPlan() {
  const physical = await listCanonicalProductDirs();
  const duplicateNumbers = [];
  for (let index = 1; index < physical.length; index += 1) {
    if (physical[index - 1].number === physical[index].number) {
      duplicateNumbers.push([physical[index - 1].name, physical[index].name]);
    }
  }
  if (duplicateNumbers.length) throw new Error(`Duplicate numeric folder identities: ${JSON.stringify(duplicateNumbers)}`);

  const quarantineNames = new Set();
  const assetCounts = [];
  for (const folder of physical) {
    const assets = await assetFiles(path.join(shopDir, folder.name));
    assetCounts.push({ folder: folder.name, count: assets.length });
    if (assets.length === 0) quarantineNames.add(folder.name);
  }

  const retained = physical.filter((folder) => !quarantineNames.has(folder.name));
  const mappings = retained.map((folder, index) => ({
    oldFolder: folder.name,
    oldNumber: folder.number,
    newFolder: canonicalProductName(index + 1),
    newNumber: index + 1,
  }));
  const mappingByOld = new Map(mappings.map((mapping) => [mapping.oldFolder, mapping]));

  const workbook = await importWorkbook(workbookPath);
  const before = await inspectWorkbook(workbook);
  const rowChanges = [];
  const staleRows = [];
  const seenFinalRefs = new Map();
  for (let rowIndex = 1; rowIndex < before.values.length; rowIndex += 1) {
    const rowNumber = rowIndex + 1;
    const folderRef = before.values[rowIndex][FOLDER_COL];
    if (typeof folderRef !== "string" || !PRODUCT_RE.test(folderRef)) continue;
    const mapping = mappingByOld.get(folderRef);
    if (!mapping) {
      staleRows.push({
        row: rowNumber,
        oldFolder: folderRef,
        oldSku: before.values[rowIndex][SKU_COL],
        reason: quarantineNames.has(folderRef) ? "quarantined-empty-folder" : "missing-physical-folder",
      });
      rowChanges.push({ rowIndex, row: rowNumber, folder: null, sku: null, kind: "clear-stale" });
      continue;
    }
    if (seenFinalRefs.has(mapping.newFolder)) {
      throw new Error(`Workbook duplicate physical ref: rows ${seenFinalRefs.get(mapping.newFolder)} and ${rowNumber} -> ${mapping.newFolder}`);
    }
    seenFinalRefs.set(mapping.newFolder, rowNumber);
    rowChanges.push({
      rowIndex,
      row: rowNumber,
      folder: mapping.newFolder,
      sku: skuForFolder(mapping.newFolder),
      kind: "mapped",
      oldFolder: folderRef,
      oldSku: before.values[rowIndex][SKU_COL],
    });
  }

  const physicalWithoutRows = mappings.filter((mapping) => !seenFinalRefs.has(mapping.newFolder));
  const retainedFileInventory = await inventoryProductFiles(retained.map((folder) => folder.name));
  const sourceWorkbook = await fileSnapshot(workbookPath);
  return {
    workbook,
    before,
    sourceWorkbook,
    physical,
    quarantineNames,
    assetCounts,
    retained,
    mappings,
    mappingByOld,
    rowChanges,
    staleRows,
    physicalWithoutRows,
    retainedFileInventory,
    renders: [],
  };
}

function applyWorkbookChanges(plan) {
  const sheet = plan.workbook.worksheets.getItem(SHEET_NAME);
  for (const change of plan.rowChanges) {
    const rowNumber = change.rowIndex + 1;
    sheet.getRange(`B${rowNumber}`).values = [[change.folder]];
    sheet.getRange(`R${rowNumber}`).values = [[change.sku]];
  }
}

async function verifyAuthoredWorkbook(plan, stagedPath) {
  const staged = await importWorkbook(stagedPath);
  const after = await inspectWorkbook(staged);
  if (!matrixEqual(plan.before.nonTargetValues, after.nonTargetValues)) {
    throw new Error("Non-target workbook values changed outside columns B/R");
  }
  if (!matrixEqual(plan.before.nonTargetFormulas, after.nonTargetFormulas)) {
    throw new Error("Non-target workbook formulas changed outside columns B/R");
  }
  if (!matrixEqual(plan.before.etsyUrls, after.etsyUrls)) {
    throw new Error("Etsy URL multiset changed");
  }
  const refs = [];
  const existingFinalFolders = new Set(plan.mappings.map((mapping) => mapping.newFolder));
  for (let rowIndex = 1; rowIndex < after.values.length; rowIndex += 1) {
    const folder = after.values[rowIndex][FOLDER_COL];
    if (typeof folder !== "string" || !PRODUCT_RE.test(folder)) continue;
    if (!existingFinalFolders.has(folder)) throw new Error(`Row ${rowIndex + 1} refers to nonexistent final folder ${folder}`);
    const sku = after.values[rowIndex][SKU_COL];
    if (sku !== skuForFolder(folder)) throw new Error(`Row ${rowIndex + 1} has invalid SKU ${JSON.stringify(sku)} for ${folder}`);
    refs.push(folder);
  }
  if (new Set(refs).size !== refs.length) throw new Error("Final workbook folder references are not unique");
  return { after, refs, renders: [] };
}

async function renameFoldersForward(plan, runId, quarantineDir) {
  const moves = [];
  await fs.mkdir(quarantineDir, { recursive: false });
  try {
    for (const emptyFolder of [...plan.quarantineNames].sort()) {
      const source = path.join(shopDir, emptyFolder);
      const destination = path.join(quarantineDir, emptyFolder);
      await fs.rename(source, destination);
      moves.push({ from: source, to: destination, phase: "quarantine" });
    }
    for (let index = 0; index < plan.mappings.length; index += 1) {
      const mapping = plan.mappings[index];
      const source = path.join(shopDir, mapping.oldFolder);
      const destination = path.join(shopDir, `.product-compaction-tmp-${runId}-${String(index + 1).padStart(3, "0")}`);
      await fs.rename(source, destination);
      mapping.tempFolder = path.basename(destination);
      moves.push({ from: source, to: destination, phase: "to-temp" });
    }
    for (const mapping of plan.mappings) {
      const source = path.join(shopDir, mapping.tempFolder);
      const destination = path.join(shopDir, mapping.newFolder);
      await fs.rename(source, destination);
      moves.push({ from: source, to: destination, phase: "to-final" });
    }
    return moves;
  } catch (error) {
    await rollbackMoves(moves);
    try { await fs.rmdir(quarantineDir); } catch {}
    throw error;
  }
}

async function rollbackMoves(moves) {
  const failures = [];
  for (const move of [...moves].reverse()) {
    try {
      await fs.rename(move.to, move.from);
    } catch (error) {
      failures.push({ move, error: error.message });
    }
  }
  if (failures.length) throw new Error(`Folder rollback incomplete: ${JSON.stringify(failures)}`);
}

async function verifyFinalFilesystem(plan) {
  const canonical = await listCanonicalProductDirs();
  const expected = Array.from({ length: plan.mappings.length }, (_, index) => canonicalProductName(index + 1));
  const actual = canonical.map((folder) => folder.name);
  if (!matrixEqual(actual, expected)) throw new Error(`Final canonical sequence mismatch: ${JSON.stringify(actual)}`);
  const empty = [];
  for (const folder of actual) {
    if ((await assetFiles(path.join(shopDir, folder))).length === 0) empty.push(folder);
  }
  if (empty.length) throw new Error(`Empty final canonical folders: ${empty.join(", ")}`);
  const postInventory = await inventoryProductFiles(actual);
  if (!matrixEqual(identityMultiset(plan.retainedFileInventory), identityMultiset(postInventory))) {
    throw new Error("Retained asset metadata identity multiset changed");
  }
  return { canonical, postInventory };
}

async function acquireLock(runId) {
  let handle;
  try {
    handle = await fs.open(lockPath, "wx", 0o600);
    await handle.writeFile(`${JSON.stringify({ pid: process.pid, runId, startedAt: new Date().toISOString() })}\n`);
    return handle;
  } catch (error) {
    if (handle) await handle.close();
    if (error.code === "EEXIST") throw new Error(`Compaction lock already exists: ${lockPath}`);
    throw error;
  }
}

function dryRunSummary(plan) {
  return {
    mode: "dry-run",
    sourceWorkbook: plan.sourceWorkbook,
    canonicalPhysicalFolders: plan.physical.length,
    quarantineFolders: [...plan.quarantineNames].sort(),
    retainedFolders: plan.retained.length,
    finalRange: [canonicalProductName(1), canonicalProductName(plan.retained.length)],
    workbookRows: plan.before.values.length,
    workbookMappedRows: plan.rowChanges.filter((change) => change.kind === "mapped").length,
    staleRowsCleared: plan.staleRows.length,
    staleRowExamples: plan.staleRows.slice(0, 10),
    physicalFoldersWithoutRows: plan.physicalWithoutRows.map((mapping) => mapping.oldFolder),
    retainedRegularFiles: plan.retainedFileInventory.length,
    datalessOrZeroBlockFiles: plan.retainedFileInventory.filter((file) => file.datalessOrZeroBlock).length,
    assetVerification: "metadata-only (relativePath, dev, ino, size, blocks, mtimeMs); content hashes skipped to avoid hydrating placeholders",
    mappingExamples: plan.mappings.slice(0, 10),
    assetCountExamples: plan.assetCounts.slice(0, 10),
    datalessExamples: plan.retainedFileInventory.filter((file) => file.datalessOrZeroBlock).slice(0, 10),
    etsyUrlCount: plan.before.etsyUrls.length,
    etsyUrlExamples: plan.before.etsyUrls.slice(0, 10),
    renders: plan.renders,
  };
}

async function runApply(plan) {
  const runId = timestamp();
  const lockHandle = await acquireLock(runId);
  const backupPath = path.join(shopDir, `Etsy_SEO_Generator.backup_product_compaction_${runId}.xlsx`);
  const stagedPath = path.join(shopDir, `.Etsy_SEO_Generator.product_compaction_${runId}.tmp.xlsx`);
  const quarantineDir = path.join(shopDir, `.product-compaction-quarantine-${runId}`);
  const pendingManifestPath = path.join(shopDir, `.product-compaction-${runId}.pending.json`);
  const manifestPath = path.join(shopDir, `product-compaction-${runId}-manifest.json`);
  let moves = [];
  let workbookReplaced = false;
  try {
    const liveAtStart = await fileSnapshot(workbookPath);
    if (!sameSnapshot(plan.sourceWorkbook, liveAtStart)) {
      throw new Error("Workbook changed between planning and apply start; rerun dry-run");
    }

    await fs.copyFile(workbookPath, backupPath, fs.constants.COPYFILE_EXCL);
    const backupSnapshot = await fileSnapshot(backupPath);
    if (backupSnapshot.sha256 !== plan.sourceWorkbook.sha256 || backupSnapshot.size !== plan.sourceWorkbook.size) {
      throw new Error("Byte-for-byte workbook backup verification failed");
    }

    applyWorkbookChanges(plan);
    const exported = await SpreadsheetFile.exportXlsx(plan.workbook);
    await exported.save(stagedPath);
    const stagedVerification = await verifyAuthoredWorkbook(plan, stagedPath);
    const stagedSnapshot = await fileSnapshot(stagedPath);

    const beforeFolders = await fileSnapshot(workbookPath);
    if (!sameSnapshot(plan.sourceWorkbook, beforeFolders)) {
      throw new Error("Workbook changed while staging; aborting before folder mutation");
    }

    const pendingManifest = {
      schemaVersion: 1,
      status: "pending",
      runId,
      shop: SHOP_SLUG,
      createdAt: new Date().toISOString(),
      sourceWorkbook: { path: workbookPath, ...plan.sourceWorkbook },
      backupWorkbook: { path: backupPath, ...backupSnapshot },
      stagedWorkbook: { path: stagedPath, ...stagedSnapshot },
      quarantineDirectory: quarantineDir,
      mappings: plan.mappings.map(({ tempFolder: _ignored, ...mapping }) => mapping),
      quarantine: [...plan.quarantineNames].sort(),
      assetCounts: plan.assetCounts,
      etsyUrls: plan.before.etsyUrls,
      staleWorkbookRows: plan.staleRows,
      retainedPreFiles: plan.retainedFileInventory,
      assetVerification: {
        method: "metadata-identity",
        fields: ["relativePath", "dev", "ino", "size", "blocks", "mtimeMs"],
        contentHashes: "skipped to avoid hydrating APFS/iCloud dataless placeholders",
        datalessOrZeroBlockCount: plan.retainedFileInventory.filter((file) => file.datalessOrZeroBlock).length,
      },
    };
    await fs.writeFile(pendingManifestPath, `${JSON.stringify(pendingManifest, null, 2)}\n`, { flag: "wx" });

    moves = await renameFoldersForward(plan, runId, quarantineDir);

    const immediatelyBeforeReplace = await fileSnapshot(workbookPath);
    if (!sameSnapshot(plan.sourceWorkbook, immediatelyBeforeReplace)) {
      await rollbackMoves(moves);
      moves = [];
      try { await fs.rmdir(quarantineDir); } catch {}
      throw new Error("Concurrent workbook change detected immediately before atomic replace; folders rolled back");
    }

    await fs.rename(stagedPath, workbookPath);
    workbookReplaced = true;

    const finalFilesystem = await verifyFinalFilesystem(plan);
    const finalWorkbook = await importWorkbook(workbookPath);
    const finalVerification = await verifyAuthoredWorkbook(plan, workbookPath);
    const finalWorkbookSnapshot = await fileSnapshot(workbookPath);
    const finalManifest = {
      ...pendingManifest,
      status: "complete",
      completedAt: new Date().toISOString(),
      finalWorkbook: { path: workbookPath, ...finalWorkbookSnapshot },
      counts: {
        preCanonicalFolders: plan.physical.length,
        quarantinedFolders: plan.quarantineNames.size,
        finalCanonicalFolders: finalFilesystem.canonical.length,
        mappedWorkbookRows: plan.rowChanges.filter((change) => change.kind === "mapped").length,
        staleWorkbookRowsCleared: plan.staleRows.length,
        retainedRegularFiles: finalFilesystem.postInventory.length,
        datalessOrZeroBlockFiles: finalFilesystem.postInventory.filter((file) => file.datalessOrZeroBlock).length,
        etsyUrls: finalVerification.after.etsyUrls.length,
      },
      retainedPostFiles: finalFilesystem.postInventory,
      verification: {
        exactCanonicalSequence: true,
        noEmptyCanonicalFolders: true,
        retainedAssetMetadataIdentityMultisetUnchanged: true,
        workbookRefsUniqueAndExisting: true,
        rowBackedSkusConsistent: true,
        etsyUrlMultisetUnchanged: true,
        nonTargetValuesAndFormulasUnchanged: true,
        sourceRender: plan.renders,
        finalRender: finalVerification.renders,
      },
    };
    await fs.writeFile(manifestPath, `${JSON.stringify(finalManifest, null, 2)}\n`, { flag: "wx" });
    await fs.unlink(pendingManifestPath);
    return {
      mode: "apply",
      status: "complete",
      workbook: workbookPath,
      workbookBackup: backupPath,
      quarantineDirectory: quarantineDir,
      manifest: manifestPath,
      verification: finalManifest.verification,
      counts: finalManifest.counts,
      sourceWorkbook: plan.sourceWorkbook,
      finalWorkbook: finalWorkbookSnapshot,
      finalWorkbookObjectLoaded: Boolean(finalWorkbook),
    };
  } catch (error) {
    if (moves.length) {
      try {
        await rollbackMoves(moves);
        moves = [];
        try { await fs.rmdir(quarantineDir); } catch {}
      } catch (rollbackError) {
        error.message += `; ${rollbackError.message}`;
      }
    }
    if (workbookReplaced) {
      try {
        await fs.copyFile(backupPath, workbookPath);
        workbookReplaced = false;
      } catch (restoreError) {
        error.message += `; workbook restore failed: ${restoreError.message}`;
      }
    }
    try { await fs.unlink(stagedPath); } catch {}
    throw error;
  } finally {
    await lockHandle.close();
    try { await fs.unlink(lockPath); } catch {}
  }
}

const plan = await buildPlan();
if (dryRunMode) {
  process.stdout.write(`${JSON.stringify(dryRunSummary(plan), null, 2)}\n`);
} else {
  process.stdout.write(`${JSON.stringify(await runApply(plan), null, 2)}\n`);
}
