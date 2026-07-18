// UI-T15 — pure-logic tests for OS-aware share formatting. Runs on Node's
// built-in test runner with native TypeScript type-stripping (Node >=22.18):
// `node --test frontend/tests/`. No bundler / DOM — exercises the DOM-free
// decision core in ../src/lib/osFormat.ts.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  detectPlatform,
  effectiveFormat,
  deriveSmbFromUnc,
  shareLocation,
  formatShare,
} from "../src/lib/osFormat.ts";

test("detectPlatform maps raw platform strings", () => {
  assert.equal(detectPlatform("Win32"), "windows");
  assert.equal(detectPlatform("Windows"), "windows");
  assert.equal(detectPlatform("MacIntel"), "mac");
  assert.equal(detectPlatform("iPhone"), "mac");
  assert.equal(detectPlatform("Linux x86_64"), "linux");
  assert.equal(detectPlatform("Android"), "linux");
  assert.equal(detectPlatform(""), "other");
  assert.equal(detectPlatform(null), "other");
  assert.equal(detectPlatform("SunOS"), "other");
});

test("effectiveFormat: auto follows OS, override always wins", () => {
  assert.equal(effectiveFormat("auto", "windows"), "unc");
  assert.equal(effectiveFormat("auto", "mac"), "url");
  assert.equal(effectiveFormat("auto", "linux"), "url");
  assert.equal(effectiveFormat("auto", "other"), "url");
  // override wins regardless of platform
  assert.equal(effectiveFormat("url", "windows"), "url");
  assert.equal(effectiveFormat("unc", "linux"), "unc");
});

test("deriveSmbFromUnc: UNC -> smb URL, subpaths + spaces preserved", () => {
  assert.equal(deriveSmbFromUnc("\\\\tower\\media"), "smb://tower/media");
  assert.equal(
    deriveSmbFromUnc("\\\\tower\\Media Management\\Movies\\x.mkv"),
    "smb://tower/Media Management/Movies/x.mkv",
  );
  assert.equal(deriveSmbFromUnc("not-a-unc"), null);
  assert.equal(deriveSmbFromUnc(null), null);
  // ipv6-literal host restored to a bracketed literal
  assert.equal(deriveSmbFromUnc("\\\\fe80--1.ipv6-literal.net\\share"), "smb://[fe80::1]/share");
});

test("shareLocation: manual UNC prefix derives the smb URL for non-Windows viewers", () => {
  const loc = shareLocation("\\\\host\\m", null);
  assert.equal(loc.url, "smb://host/m");
  assert.equal(loc.unc, "\\\\host\\m");
});

test("shareLocation: smb prefix + server-supplied unc passes both through", () => {
  const loc = shareLocation("smb://tower/media", "\\\\tower\\media");
  assert.equal(loc.url, "smb://tower/media");
  assert.equal(loc.unc, "\\\\tower\\media");
});

test("shareLocation: sftp/posix prefix has no unc", () => {
  assert.deepEqual(shareLocation("sftp://h/p", null), { url: "sftp://h/p", unc: null });
  assert.deepEqual(shareLocation("/Volumes/media", null), { url: "/Volumes/media", unc: null });
});

test("formatShare: picks the OS-appropriate spelling", () => {
  const loc = { url: "smb://tower/media", unc: "\\\\tower\\media" };
  assert.equal(formatShare(loc, "auto", "windows"), "\\\\tower\\media");
  assert.equal(formatShare(loc, "auto", "linux"), "smb://tower/media");
  assert.equal(formatShare(loc, "url", "windows"), "smb://tower/media"); // override wins
  assert.equal(formatShare(loc, "unc", "linux"), "\\\\tower\\media"); // override wins
});

test("formatShare: graceful fallback when the wanted spelling is null", () => {
  const noUnc = { url: "sftp://h/p", unc: null };
  // UNC requested (or Windows-auto) but none exists -> fall back to url
  assert.equal(formatShare(noUnc, "unc", "linux"), "sftp://h/p");
  assert.equal(formatShare(noUnc, "auto", "windows"), "sftp://h/p");
  const noUrl = { url: null, unc: "\\\\h\\s" };
  assert.equal(formatShare(noUrl, "url", "linux"), "\\\\h\\s");
  assert.equal(formatShare(null, "auto", "linux"), null);
  assert.equal(formatShare({ url: null, unc: null }, "auto", "linux"), null);
});
