import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const inputPath = "holdings.xlsx";
const outputPath = "holdings_public.xlsx";
const keepHeaders = ["Symbol", "ISIN", "Instrument Type"];

const input = await FileBlob.load(inputPath);
const source = await SpreadsheetFile.importXlsx(input);
const sourceSheet = source.worksheets.getItemAt(0);
const used = sourceSheet.getUsedRange(true);
const values = used.values;

const headerIndex = values.findIndex(
  (row) => row.includes("Symbol") && row.includes("ISIN") && row.includes("Instrument Type"),
);

if (headerIndex < 0) {
  throw new Error("Could not find holdings header row with Symbol, ISIN, and Instrument Type.");
}

const headers = values[headerIndex].map((value) => String(value ?? "").trim());
const columnIndexes = keepHeaders.map((header) => headers.indexOf(header));
if (columnIndexes.some((idx) => idx < 0)) {
  throw new Error(`Missing one or more required headers: ${keepHeaders.join(", ")}`);
}

const rows = [keepHeaders];
for (const row of values.slice(headerIndex + 1)) {
  const publicRow = columnIndexes.map((idx) => String(row[idx] ?? "").trim());
  const [name, isin, instrumentType] = publicRow;
  if (!name || !isin) continue;
  if (/\barbitrage\b/i.test(`${name} ${instrumentType}`)) continue;
  rows.push(publicRow);
}

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("Holdings");
sheet.getRangeByIndexes(0, 0, rows.length, keepHeaders.length).values = rows;
sheet.freezePanes.freezeRows(1);
sheet.showGridLines = false;

const headerRange = sheet.getRangeByIndexes(0, 0, 1, keepHeaders.length);
headerRange.format.fill.color = "#EAF2F8";
headerRange.format.font.bold = true;
headerRange.format.font.color = "#1F2933";

const tableRange = sheet.getRangeByIndexes(0, 0, rows.length, keepHeaders.length);
tableRange.format.borders = { preset: "inside", style: "thin", color: "#D8DEE8" };
tableRange.format.autofitColumns();
sheet.getRange("A:A").format.columnWidth = 52;
sheet.getRange("B:B").format.columnWidth = 18;
sheet.getRange("C:C").format.columnWidth = 28;

const preview = await workbook.render({ sheetName: "Holdings", autoCrop: "all", scale: 1, format: "png" });
await fs.writeFile("/tmp/holdings_public_preview.png", new Uint8Array(await preview.arrayBuffer()));

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 50 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(`Saved ${outputPath} with ${rows.length - 1} fund rows.`);
