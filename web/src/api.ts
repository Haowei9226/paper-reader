export async function api<T=any>(path:string, options:RequestInit={}):Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type','application/json');
  let response:Response;
  try { response = await fetch('/api'+path, {...options,headers}); }
  catch { throw new Error('无法连接文献阅读器，请检查本机服务是否仍在运行'); }
  if(!response.ok) {
    const body=await response.json().catch(()=>({}));
    throw new Error(typeof body.detail==='string'?body.detail:`请求失败（${response.status}）`);
  }
  return response.json();
}
export const post=<T=any>(path:string,value:unknown={})=>api<T>(path,{method:'POST',body:JSON.stringify(value)});
export const patch=<T=any>(path:string,value:unknown)=>api<T>(path,{method:'PATCH',body:JSON.stringify(value)});
export const remove=(path:string)=>api(path,{method:'DELETE'});
export const date=(v:number)=>new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Europe/London'}).format(v*1000);
export const filesize=(bytes:number)=>bytes>1024**3?(bytes/1024**3).toFixed(1)+' GB':bytes>1024**2?(bytes/1024**2).toFixed(1)+' MB':Math.ceil(bytes/1024)+' KB';
