/**
 * What kind of file an attachment is, for picking its icon and label.
 *
 * Attachments without a preview (PDFs, spreadsheets, archives, ...) used to
 * all render as the same paperclip, so a user scrolling back through a
 * session could not tell one upload from another. The kind drives a distinct
 * icon; the extension is shown next to the filename.
 */

export type FileKind =
  | "pdf"
  | "document"
  | "spreadsheet"
  | "presentation"
  | "code"
  | "data"
  | "archive"
  | "audio"
  | "video"
  | "image"
  | "text"
  | "other";

const EXTENSION_KINDS: Record<string, FileKind> = {
  pdf: "pdf",
  doc: "document",
  docx: "document",
  odt: "document",
  rtf: "document",
  pages: "document",
  xls: "spreadsheet",
  xlsx: "spreadsheet",
  xlsm: "spreadsheet",
  ods: "spreadsheet",
  csv: "spreadsheet",
  tsv: "spreadsheet",
  numbers: "spreadsheet",
  ppt: "presentation",
  pptx: "presentation",
  odp: "presentation",
  key: "presentation",
  json: "data",
  jsonl: "data",
  yaml: "data",
  yml: "data",
  toml: "data",
  xml: "data",
  parquet: "data",
  sqlite: "data",
  db: "data",
  zip: "archive",
  tar: "archive",
  gz: "archive",
  tgz: "archive",
  bz2: "archive",
  xz: "archive",
  "7z": "archive",
  rar: "archive",
  mp3: "audio",
  wav: "audio",
  m4a: "audio",
  flac: "audio",
  ogg: "audio",
  aac: "audio",
  mp4: "video",
  mov: "video",
  webm: "video",
  mkv: "video",
  avi: "video",
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  heic: "image",
  svg: "image",
  bmp: "image",
  txt: "text",
  md: "text",
  markdown: "text",
  log: "text",
  py: "code",
  js: "code",
  jsx: "code",
  ts: "code",
  tsx: "code",
  html: "code",
  css: "code",
  java: "code",
  go: "code",
  rs: "code",
  c: "code",
  h: "code",
  cpp: "code",
  cs: "code",
  rb: "code",
  php: "code",
  sh: "code",
  sql: "code",
  ipynb: "code",
};

/** Lower-case extension without the dot, or "" when the name has none. */
export function fileExtension(filename: string): string {
  const dot = filename.lastIndexOf(".");
  if (dot <= 0 || dot === filename.length - 1) {
    return "";
  }
  return filename.slice(dot + 1).toLowerCase();
}

export function fileKind(filename: string, mediaType?: string): FileKind {
  const byExtension = EXTENSION_KINDS[fileExtension(filename)];
  if (byExtension) {
    return byExtension;
  }
  if (!mediaType) {
    return "other";
  }
  if (mediaType === "application/pdf") {
    return "pdf";
  }
  if (mediaType.startsWith("image/")) {
    return "image";
  }
  if (mediaType.startsWith("video/")) {
    return "video";
  }
  if (mediaType.startsWith("audio/")) {
    return "audio";
  }
  if (mediaType.startsWith("text/")) {
    return "text";
  }
  return "other";
}
