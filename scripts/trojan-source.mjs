// Invoke the independently hash-pinned upstream library on literal blob text.
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const {hasConfusables} = await import(pathToFileURL(process.env.CHAINMAN_TROJAN_SOURCE).href);
const blobs = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(blobs.map(sourceText => hasConfusables({sourceText, detailed: true}))));
