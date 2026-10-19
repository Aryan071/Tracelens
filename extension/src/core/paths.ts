/** Index paths are repository-relative ("demo/src/orders.ts"). An open document is matched to them by
 * its longest common trailing path, so it works whether the workspace is the repository root, the
 * `demo/` folder or a checkout elsewhere. */
export function normalizePath(p: string): string {
  return p.replace(/\\/g, "/").replace(/^\.?\//, "");
}

export function commonSuffixSegments(a: string, b: string): number {
  const x = normalizePath(a).split("/");
  const y = normalizePath(b).split("/");
  let n = 0;
  while (n < x.length && n < y.length && x[x.length - 1 - n] === y[y.length - 1 - n]) n++;
  return n;
}

/** True when `docPath` (absolute, or relative to the workspace folder) is the file the index calls
 * `indexPath`: either the index path is a trailing part of the document's path (workspace is deeper
 * than the repository, or elsewhere on disk), or the document's relative path is a trailing part of
 * the index path (workspace opened at a subfolder such as `demo/`). A bare file name only matches a
 * bare file name, so `utils.ts` in one folder is not mistaken for `a/utils.ts`. */
export function sameFile(docPath: string, indexPath: string): boolean {
  const d = normalizePath(docPath).split("/").length;
  const n = normalizePath(indexPath).split("/").length;
  const h = commonSuffixSegments(docPath, indexPath);
  return h === n || (h === d && h >= Math.min(2, n));
}
