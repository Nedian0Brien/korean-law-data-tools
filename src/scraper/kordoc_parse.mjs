/**
 * kordoc_parse.mjs
 * HWP/HWPX 파일을 읽어 Markdown으로 변환 후 stdout에 출력
 * 사용법: node kordoc_parse.mjs <파일경로>
 */
import { readFileSync } from "fs";
import { existsSync } from "fs";

let parse;
const errors = [];

try {
  const kordoc = await import("kordoc");
  parse = kordoc.parse;
} catch (err1) {
  errors.push(`package import: ${err1?.message || "unknown"}`);

  const fallbackCandidates = [
    new URL("../../node_modules/kordoc/dist/index.js", import.meta.url),
    new URL("../node_modules/kordoc/dist/index.js", import.meta.url),
    new URL("./node_modules/kordoc/dist/index.js", import.meta.url),
  ];

  for (const candidate of fallbackCandidates) {
    if (!existsSync(candidate)) {
      continue;
    }
    try {
      const fallback = await import(candidate);
      if (fallback && typeof fallback.parse === "function") {
        parse = fallback.parse;
        break;
      }
    } catch (err2) {
      errors.push(`fallback (${candidate}): ${err2?.message || "unknown"}`);
    }
  }

  if (!parse) {
    process.stderr.write("오류: kordoc 모듈을 찾거나 로드할 수 없습니다.\n");
    process.stderr.write(`${errors.join("\\n")}\n`);
    process.stderr.write("설치 방법: npm install kordoc 또는 npm install korean-law-mcp\n");
    process.exit(1);
  }
}

const filePath = process.argv[2];
if (!filePath) {
  process.stderr.write("파일 경로를 인자로 전달하세요.\n");
  process.exit(1);
}

const buffer = readFileSync(filePath);
const result = await parse(buffer.buffer);

if (!result.success) {
  process.stderr.write(`파싱 실패: ${result.error || "알 수 없는 오류"}\n`);
  process.exit(1);
}

process.stdout.write(result.markdown || "");
