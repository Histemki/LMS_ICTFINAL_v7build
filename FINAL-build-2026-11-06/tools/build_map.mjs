import { readdir, readFile, writeFile, mkdir, rm } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = path.dirname(fileURLToPath(import.meta.url));
const source = path.join(root, 'map_source');
const output = path.join(root, '..', 'static', 'map', 'svg');
await mkdir(output, { recursive: true });
for (const stale of ['floor-NaN-all.svg','floor-NaN-all-ios.svg','floor-NaN-C1.1.svg','floor-NaN-C1.2.svg','floor-NaN-C1.3.svg']) await rm(path.join(output, stale), { force: true });
const attrNames = { className: 'class', strokeWidth: 'stroke-width', strokeLinecap: 'stroke-linecap', strokeLinejoin: 'stroke-linejoin', strokeMiterlimit: 'stroke-miterlimit', fillRule: 'fill-rule', clipRule: 'clip-rule', fontSize: 'font-size', textAnchor: 'text-anchor' };
function convert(text, filename, viewBox) {
  const floorSource = filename.match(/C1_(?:ALL|IOS)_(\d)\.jsx$/)?.[1];
  const blockSource = filename.match(/C1_([123])_([123])\.jsx$/);
  const floor = Number(blockSource ? blockSource[2] : floorSource);
  const wrapperEnd = text.indexOf('</LayoutMinimapMiddle>') >= 0 ? text.indexOf('</LayoutMinimapMiddle>') : text.indexOf('</LayoutMinimap>') >= 0 ? text.indexOf('</LayoutMinimap>') : text.indexOf('</MapLayout>');
  const rootStart = text.indexOf('<g id=');
  if (rootStart < 0 || wrapperEnd < 0) throw new Error('Could not find SVG content in '+filename);
  let svg = text.slice(rootStart, wrapperEnd).trim();
  svg = svg.replace(/\{\s*\/\*[\s\S]*?\*\/\s*\}/g, '');
  svg = svg.replace(/<([A-Z][A-Za-z0-9_.]*)\b[^>]*\/>/g, '');
  svg = svg.replace(/<\/?[A-Z][A-Za-z0-9_.]*\b[^>]*>/g, '');
  svg = svg.replace(/\s+(\w+)\s*=\s*\{(?:"([^"]*)"|'([^']*)'|([^{}]*))\}/g, (_, key, dq, sq, raw) => ` ${attrNames[key] || key}="${(dq ?? sq ?? raw ?? '').trim()}"`);
  svg = svg.replace(/\s+(\w+)\s*=\s*"([^"]*)"/g, (_, key, value) => ` ${attrNames[key] || key}="${value}"`);
  svg = svg.replace(/\s+(\w+)\s*=\s*'([^']*)'/g, (_, key, value) => ` ${attrNames[key] || key}="${value}"`);
  svg = svg.replace(/\s+\/?>/g, (tag) => tag);
  const out = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}" role="img" data-floor="${floor}" aria-label="${floor} floor university plan">${svg}</svg>`;
  return { floor, out };
}
const files = (await readdir(source)).filter(f => f.endsWith('.jsx'));
for (const file of files) {
  const raw = await readFile(path.join(source, file), 'utf8');
  if (file.startsWith('C1_IOS_')) {
    const { floor, out } = convert(raw, file, '0 0 924.69 396.16');
    await writeFile(path.join(output, `floor-${floor}-all-ios.svg`), out);
  } else if (file.startsWith('C1_ALL_')) {
    const { floor, out } = convert(raw, file, '0 0 924.69 396.16');
    await writeFile(path.join(output, `floor-${floor}-all.svg`), out);
  } else {
    const { floor, out } = convert(raw, file, '0 0 347.52 354.54');
    const block = file.match(/C1_([123])_/)[1];
    await writeFile(path.join(output, `floor-${floor}-C1.${block}.svg`), out);
  }
}
console.log(`Converted ${files.length} JSX plans to SVG.`);
