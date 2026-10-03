// Ground truth for call resolution: resolve every call/new expression in a TS project with the
// real TypeScript type checker and print where each resolves to (declarations inside the repo).
//   node bench/tsc_oracle.js <repo> <tsconfig relative to repo> <src prefix relative to repo>
const ts = require(process.env.TS_PATH || "typescript");
const path = require("path");
const [repo, tsconfig, prefix] = process.argv.slice(2);
const cfgFile = path.join(repo, tsconfig);
const raw = ts.readConfigFile(cfgFile, ts.sys.readFile);
// The config may `extends` a package that is not installed, so take its own compilerOptions only.
const conv = ts.convertCompilerOptionsFromJson(raw.config.compilerOptions || {}, path.dirname(cfgFile));
const options = {
  target: ts.ScriptTarget.ES2022, lib: ["lib.es2022.d.ts"], module: ts.ModuleKind.CommonJS,
  moduleResolution: ts.ModuleResolutionKind.Node10, esModuleInterop: true,
  ...conv.options, noEmit: true, skipLibCheck: true, types: [], outDir: undefined, declaration: false,
};
const absPrefix = path.join(repo, prefix) + path.sep;
const roots = ts.sys.readDirectory(absPrefix.slice(0, -1), [".ts", ".tsx"], ["**/node_modules/**"])
  .filter((f) => !f.endsWith(".d.ts"));
const program = ts.createProgram(roots, options);
const checker = program.getTypeChecker();
const rel = (f) => path.relative(repo, f).split(path.sep).join("/");
const out = [];
let calls = 0, resolved = 0;
// An overload signature has no body; the indexer (like a debugger) cares about the implementation.
function implementationOf(decl) {
  const isFn = ts.isFunctionDeclaration(decl) || ts.isMethodDeclaration(decl) || ts.isConstructorDeclaration(decl);
  if (!isFn || decl.body) return decl;
  if (ts.isConstructorDeclaration(decl)) {
    const impl = decl.parent.members.find((m) => ts.isConstructorDeclaration(m) && m.body);
    return impl || decl;
  }
  const sym = decl.name && checker.getSymbolAtLocation(decl.name);
  const impl = sym && sym.declarations && sym.declarations.find((d) => d.body && d.kind === decl.kind);
  return impl || decl;
}
function visit(sf, node) {
  if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
    calls++;
    let sig;
    try { sig = checker.getResolvedSignature(node); } catch (e) { sig = undefined; }
    const decl0 = sig && sig.declaration;
    const decl = decl0 && implementationOf(decl0);
    if (decl && decl.getSourceFile() && !decl.getSourceFile().isDeclarationFile) {
      const dsf = decl.getSourceFile();
      if (rel(dsf.fileName).startsWith(prefix)) {
        resolved++;
        const nameNode = decl.name || decl;
        out.push({
          file: rel(sf.fileName),
          line: sf.getLineAndCharacterOfPosition(node.getStart()).line + 1,
          kind: ts.isNewExpression(node) ? "new" : "call",
          declFile: rel(dsf.fileName),
          declName: decl.name ? decl.name.getText() : ts.SyntaxKind[decl.kind],
          anonymous: !decl.name,
          declNameLine: dsf.getLineAndCharacterOfPosition(nameNode.getStart()).line + 1,
        });
      }
    }
  }
  ts.forEachChild(node, (c) => visit(sf, c));
}
for (const sf of program.getSourceFiles()) {
  if (sf.isDeclarationFile) continue;
  if (!rel(sf.fileName).startsWith(prefix)) continue;
  visit(sf, sf);
}
process.stderr.write(`files=${roots.length} call_sites=${calls} resolved_to_repo_decl=${resolved}\n`);
process.stdout.write(JSON.stringify(out));
