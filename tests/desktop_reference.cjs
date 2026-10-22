// Test-only loader: execute the real desktop sources, replacing environment
// adapters (Electron/session/systeminformation) rather than server logic.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const {EventEmitter} = require('node:events');
const root = path.resolve(process.argv[2]);
const mode = process.argv[3] || 'server';
const desktopRequire = createRequire(path.join(root, 'package.json'));
const ts = process.env.IPX_TYPESCRIPT_PATH ? require(process.env.IPX_TYPESCRIPT_PATH) : desktopRequire('typescript');
const http = require('node:http');
const cache = new Map();
const realNow = Date.now;
Date.now = () => {
  if (process.env.PARITY_CLOCK_FILE) {
    try { return Number(fs.readFileSync(process.env.PARITY_CLOCK_FILE, 'utf8')); } catch {}
  }
  return realNow();
};
// Freeze the OS adapter for deterministic scoring fixtures on every CI OS.
const sourceProcess = Object.create(process);
Object.defineProperty(sourceProcess, 'platform', {value: 'win32'});
const snapshot = JSON.parse(process.env.PARITY_HARDWARE || '{}');
const si = {
  cpu: async () => snapshot.cpu, mem: async () => snapshot.mem,
  cpuFlags: async () => snapshot.flags,
  graphics: async () => ({controllers: snapshot.controllers || []}),
};
const auth = {getSession: async () => ({access_token: 'host-session'}), issueProxyToken: async () => 'admin'};
function load(file) {
  file = path.resolve(file);
  if (cache.has(file)) return cache.get(file).exports;
  const module = {exports: {}};
  cache.set(file, module);
  const source = fs.readFileSync(file, 'utf8');
  const js = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true}}).outputText;
  function localRequire(id) {
    if (id === 'electron') return {app: {getPath: () => process.env.PARITY_LOG_DIR, isPackaged: false}};
    if (id.endsWith('/auth.js')) return auth;
    if (id === 'systeminformation') return si;
    if (id === 'child_process') return {execFile: (_a, _b, _c, cb) => cb(new Error('deterministic snapshot'))};
    if (id === 'https') return {request: (_url, options, cb) => http.request(process.env.PARITY_VERIFY_URL, options, cb)};
    if (id === 'fs') {
      const proxy = Object.create(fs);
      proxy.readdirSync = (dir, ...args) => String(dir).replaceAll('\\', '/').endsWith('lib/ollama')
        ? snapshot.backend === 'unknown' ? (() => {throw new Error('missing build')})() : snapshot.backend === 'cuda' ? ['cuda_v12'] : snapshot.backend === 'rocm' ? ['rocm'] : []
        : fs.readdirSync(dir, ...args);
      return proxy;
    }
    if (id === 'http') {
      const proxy = Object.create(http);
      proxy.request = (url, options, cb) => {
        const u = new URL(url);
        if (u.origin === 'http://localhost:11434') {
          const base = new URL(process.env.PARITY_OLLAMA_URL);
          u.host = base.host;
        }
        return http.request(u, options, cb);
      };
      return proxy;
    }
    if (id.startsWith('.')) return load(path.resolve(path.dirname(file), id.replace(/\.js$/, '.ts')));
    return desktopRequire(id);
  }
  vm.runInThisContext(`(function(require,module,exports,__filename,__dirname,process){${js}\n})`, {filename: file})(localRequire,module,module.exports,file,path.dirname(file),sourceProcess);
  return module.exports;
}
(async () => {
  const helper = path.join(root, 'node-apis/helper');
  if (mode === 'rating') {
    const sys = load(path.join(helper, 'sysinfo.ts'));
    await sys.initHardwareInfo();
    const ratings = [];
    for (const size of JSON.parse(process.env.PARITY_SIZES)) ratings.push(await sys.getHardwareRating(size));
    console.log('RESULT ' + JSON.stringify(ratings));
  } else if (mode === 'client') {
    const fetch = load(path.join(helper, 'proxy52458Client.ts')).createSecure52458Fetch(async () => 'admin');
    const response = await fetch('http://127.0.0.1:52458/api/chat', {method:'POST',body:JSON.stringify({model:'test',messages:[]})});
    console.log('RESULT ' + JSON.stringify({status:response.status,body:await response.text()}));
  } else if (mode === 'vectors') {
    const crypto = require('node:crypto');
    const derive = load(path.join(helper, 'ecdhAesSession.ts')).deriveIpcSessionKey;
    const a = crypto.createECDH('prime256v1'), b = crypto.createECDH('prime256v1');
    a.setPrivateKey(Buffer.from('01'.padStart(64,'0'),'hex')); b.setPrivateKey(Buffer.from('02'.padStart(64,'0'),'hex'));
    const secret = a.computeSecret(b.getPublicKey());
    const nonce = Buffer.alloc(16,3).toString('base64url');
    const key = derive(secret,'challenge',nonce,nonce);
    const iv = Buffer.alloc(12,4), cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
    const data = Buffer.concat([cipher.update('desktop vector'),cipher.final()]);
    console.log('RESULT '+JSON.stringify({a:a.getPublicKey().toString('hex'),b:b.getPublicKey().toString('hex'),secret:secret.toString('hex'),key:key.toString('hex'),nonce,
      envelope:{type:'enc',iv:iv.toString('base64url'),tag:cipher.getAuthTag().toString('base64url'),data:data.toString('base64url')}}));
  } else {
    const sys = load(path.join(helper, 'sysinfo.ts'));
    await sys.initHardwareInfo();
    const {server} = load(path.join(helper, 'server.ts')).startProxyServer(0, JSON.parse(process.env.PARITY_USERS), ['key']);
    server.on('listening', () => console.log('READY ' + server.address().port));
  }
})().catch(e => {console.error(e);process.exit(1)});
