/**
 * Lab helper for 07_openbrand_measured.py — OpenBrand only (no Firecrawl/dembrandt).
 * Run via: pnpm exec tsx <this-file> <url>
 */
import { pathToFileURL } from "node:url";

const PGBLANK_OPENBRAND =
  "/home/babi/pgblank/tools/brand-extract-compare/src/adapters/openbrand.ts";

async function main(): Promise<number> {
  const url = process.argv[2];
  if (!url) {
    console.error("Usage: tsx _openbrand_runner.ts <url>");
    return 2;
  }
  const mod = await import(pathToFileURL(PGBLANK_OPENBRAND).href);
  const result = await mod.extractOpenBrand(url);
  console.log(JSON.stringify(result));
  return result.ok ? 0 : 1;
}

main().then((code) => process.exit(code));
