import { fromBuffer } from "yauzl";
import type { Entry, ZipFile } from "yauzl";
import { basename } from "node:path";
import { relativePath } from "./model.js";

/** Extract only expected bundle bytes. Archive manifests and tarballs have no authority. */
export async function bundleArchive(
  bytes: Buffer,
  expected: readonly string[],
): Promise<Map<string, Buffer>> {
  if (new Set(expected).size !== expected.length)
    throw new Error("Ambiguous bundle basenames in local manifest");
  const archive = await new Promise<ZipFile>((done, reject) => {
    fromBuffer(
      bytes,
      { lazyEntries: true, decodeStrings: true, strictFileNames: true },
      (error, zip) => {
        if (error || !zip) reject(error ?? new Error("Missing ZIP archive"));
        else done(zip);
      },
    );
  });
  return await new Promise((done, reject) => {
    const seen = new Set<string>(),
      selected = new Map<string, Buffer>();
    let total = 0,
      count = 0;
    const fail = (error: unknown): void => {
      archive.close();
      reject(error);
    };
    archive.on("error", fail);
    archive.on("end", () => {
      if (selected.size !== expected.length)
        reject(
          new Error("Qualified archive is missing expected signed bundles"),
        );
      else done(selected);
    });
    archive.on("entry", (entry: Entry) => {
      try {
        const directory = entry.fileName.endsWith("/"),
          name = relativePath(
            directory ? entry.fileName.slice(0, -1) : entry.fileName,
          );
        if (seen.has(name) || ++count > 10000)
          throw new Error("Duplicate or excessive qualified archive entries");
        seen.add(name);
        const mode = (entry.externalFileAttributes >>> 16) & 0xf000;
        if (mode !== 0 && mode !== (directory ? 0x4000 : 0x8000))
          throw new Error(
            "Qualified archive contains a symlink or special file",
          );
        if (entry.generalPurposeBitFlag & 1)
          throw new Error(
            "Encrypted qualified archive entries are not supported",
          );
        if (directory || !expected.includes(basename(name))) {
          archive.readEntry();
          return;
        }
        const key = basename(name);
        if (
          selected.has(key) ||
          entry.uncompressedSize > 16 * 1024 * 1024 ||
          total + entry.uncompressedSize > 64 * 1024 * 1024
        )
          throw new Error("Ambiguous or excessive qualified bundle bytes");
        total += entry.uncompressedSize;
        archive.openReadStream(entry, (error, stream) => {
          if (error || !stream) {
            fail(error ?? new Error("Unable to read qualified bundle"));
            return;
          }
          const chunks: Buffer[] = [];
          let length = 0;
          stream.on("data", (chunk: Buffer) => {
            length += chunk.length;
            if (length > entry.uncompressedSize)
              fail(new Error("ZIP bundle size mismatch"));
            else chunks.push(chunk);
          });
          stream.on("error", fail);
          stream.on("end", () => {
            selected.set(key, Buffer.concat(chunks));
            archive.readEntry();
          });
        });
      } catch (error) {
        fail(error);
      }
    });
    archive.readEntry();
  });
}
