// Offline, no vendor calls. Semantic judgments remain explicitly pending.
const fs=require('fs'),path=require('path'),crypto=require('crypto');
(async()=>{
 const root=path.resolve(__dirname,'..'),raw=fs.readFileSync(path.join(root,'evaluation/hard-cases-v1.jsonl'));
 const manifest=JSON.parse(fs.readFileSync(path.join(root,'evaluation/manifest-v1.json')));
 const sha=crypto.createHash('sha256').update(raw).digest('hex');if(sha!==manifest.sha256)throw new Error('Frozen corpus changed; create v2 instead');
 const {classifyInstruction}=await import('data:text/javascript;base64,'+fs.readFileSync(path.join(root,'web/commands.js')).toString('base64'));
 const rows=raw.toString().trim().split('\n').map(JSON.parse),results=rows.filter(c=>c.type==='instruction').map(c=>{const actual=classifyInstruction(c.input);return {id:c.id,status:actual.kind===c.expected.kind&&actual.index===c.expected.index?'pass':'fail',actual,expected:c.expected};});
 const output={corpus_sha256:sha,scope:'offline_instruction_routing_only',results,passed:results.filter(r=>r.status==='pass').length,failed:results.filter(r=>r.status==='fail').length,pending_semantic:rows.filter(c=>c.type==='human_semantic').map(c=>c.id)};
 const destination=process.argv[2];if(destination)fs.writeFileSync(destination,JSON.stringify(output,null,2)+'\n');console.log(JSON.stringify(output));if(output.failed)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1});
