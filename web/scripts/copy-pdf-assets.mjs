import {cp,mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const target=fileURLToPath(new URL('../public/pdf-assets/',import.meta.url));
await mkdir(target,{recursive:true});
for(const name of ['cmaps','standard_fonts'])await cp(fileURLToPath(new URL('../node_modules/pdfjs-dist/'+name,import.meta.url)),target+'/'+name,{recursive:true});
