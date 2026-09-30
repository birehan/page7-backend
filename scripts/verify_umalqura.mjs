#!/usr/bin/env node
/**
 * Emits a small Umm al-Qura → Gregorian fixture from Node's ICU
 * (`Intl.DateTimeFormat` with `-u-ca-islamic-umalqura`), used by
 * tests/unit/test_umalqura.py to prove frontend parity.
 *
 * Usage: node scripts/verify_umalqura.mjs > tests/fixtures/umalqura_node_parity.json
 */
const fmt = new Intl.DateTimeFormat("en-US-u-ca-islamic-umalqura", {
  year: "numeric",
  month: "numeric",
  day: "numeric",
  timeZone: "UTC",
});

function hijriPartsUTC(date) {
  const parts = Object.fromEntries(fmt.formatToParts(date).map((p) => [p.type, Number(p.value)]));
  return { year: parts.year, month: parts.month, day: parts.day };
}

function hijriToGregorianUTC(hijriYear, hijriMonth, hijriDay) {
  const estGregorianYear = Math.floor(hijriYear * 0.970224 + 621.5738);
  const anchor = Date.UTC(estGregorianYear - 1, 0, 1);
  for (let i = 0; i < 900; i++) {
    const candidate = new Date(anchor + i * 86400000);
    const parts = hijriPartsUTC(candidate);
    if (parts.year === hijriYear && parts.month === hijriMonth && parts.day === hijriDay) {
      return candidate.toISOString().slice(0, 10);
    }
  }
  throw new Error(`Could not resolve Hijri date ${hijriYear}-${hijriMonth}-${hijriDay}`);
}

const targets = [
  [1447, 9, 1],
  [1447, 10, 1],
  [1447, 12, 10],
  [1448, 9, 1],
  [1448, 10, 1],
  [1448, 12, 10],
  [1449, 9, 1],
  [1449, 10, 1],
  [1449, 12, 10],
];

const dates = targets.map(([y, m, d]) => ({
  hijri: [y, m, d],
  gregorian: hijriToGregorianUTC(y, m, d),
}));

process.stdout.write(JSON.stringify({ generatedBy: "scripts/verify_umalqura.mjs", dates }, null, 2) + "\n");
