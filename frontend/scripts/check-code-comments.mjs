/** 检查自有前端类、方法及具名函数是否具备中文用途说明。 */
import ts from "typescript";
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const chinese = /[\u4e00-\u9fff]/;

/** 枚举指定源码和测试目录，排除依赖与构建产物。 */
function files(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? files(path.join(dir, entry.name))
      : /\.(ts|tsx|mjs|js)$/.test(entry.name)
        ? [path.join(dir, entry.name)]
        : [],
  );
}

const missing = [];
let count = 0;
for (const file of [
  ...files(path.join(root, "src")),
  ...files(path.join(root, "tests")),
  ...files(path.join(root, "scripts")),
]) {
  const source = readFileSync(file, "utf8");
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
  /** 检查具名声明及方法的文档注释，变量函数读取其完整声明的说明。 */
  function visit(node) {
    let name;
    let target = node;
    if (
      ts.isFunctionDeclaration(node) ||
      ts.isClassDeclaration(node) ||
      ts.isMethodDeclaration(node) ||
      ts.isConstructorDeclaration(node) ||
      ts.isGetAccessor(node) ||
      ts.isSetAccessor(node)
    ) {
      name = node.name?.getText(tree) || "constructor";
    }
    if (
      ts.isVariableDeclaration(node) &&
      node.initializer &&
      (ts.isArrowFunction(node.initializer) ||
        ts.isFunctionExpression(node.initializer))
    ) {
      name = node.name.getText(tree);
      target = ts.isVariableStatement(node.parent.parent)
        ? node.parent.parent
        : node;
    }
    if (name) {
      count++;
      const docs = ts.getJSDocCommentsAndTags(target);
      if (!docs.some((doc) => chinese.test(doc.getText(tree)))) {
        missing.push({
          file: path.relative(root, file),
          line:
            tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1,
          name,
        });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
}
const report = { declarations: count, missing, passed: missing.length === 0 };
mkdirSync(path.join(root, "../docs/stages/artifacts"), { recursive: true });
writeFileSync(
  path.join(root, "../docs/stages/artifacts/modules-frontend-comments.json"),
  JSON.stringify(report, null, 2),
);
console.log(JSON.stringify(report));
process.exitCode = report.passed ? 0 : 1;
