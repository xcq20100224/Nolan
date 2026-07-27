// Nolan 网页版一键开发启动器
// 先拉起 Python 标准库后端（server.py，端口 7101），再启动 Vite 前端，
// 退出时负责把后端子进程树一并清理掉。
import { spawn, spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

// 项目根目录（本文件位于 <root>/scripts/dev.mjs）
const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// 运行后端的 Python 解释器。
// 可用环境变量 NOLAN_PYTHON 覆盖为你自己的 Python（需已安装 jarvis 依赖），
// 例如：set NOLAN_PYTHON=C:\Python311\python.exe
const PYTHON_EXE =
  process.env.NOLAN_PYTHON ||
  "C:\\Users\\J1896\\AppData\\Roaming\\kimi-desktop\\daimon-share\\daimon\\runtime\\python\\.venv\\Scripts\\python.exe";

// 启动 Python 后端：python server.py 7101，工作目录为项目根
const backend = spawn(PYTHON_EXE, ["-u", "server.py", "7101"], {
  cwd: projectRoot,
  stdio: "inherit",
  shell: false,
});

backend.on("error", (err) => {
  console.error("[dev] Python 后端启动失败：", err.message);
});

// 前端进程句柄（先声明，避免后端在等待期内退出时引用未初始化变量）
let frontend = null;

// 等待后端就绪（简单延时，标准库 http.server 启动很快）
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
await wait(1500);

// 若后端在等待期内就已退出（exit 监听尚未挂载，事件可能丢失），直接报错退出
if (backend.exitCode !== null) {
  console.error(`[dev] Python 后端启动后立即退出（code=${backend.exitCode}），请检查 server.py 与端口占用`);
  process.exit(backend.exitCode ?? 1);
}

// 启动 Vite，把 CLI 参数原样透传（如 npm run dev -- --port x --host）
// 直接用 node 跑 vite.js 入口，避开 Windows 下 spawn .cmd 的兼容性问题
const viteEntry = path.join(projectRoot, "node_modules", "vite", "bin", "vite.js");
frontend = spawn(process.execPath, [viteEntry, ...process.argv.slice(2)], {
  cwd: projectRoot,
  stdio: "inherit",
  shell: false,
});

frontend.on("error", (err) => {
  console.error("[dev] Vite 启动失败：", err.message);
});

// 清理函数：杀掉 Python 后端整个进程树（Windows 用 taskkill /T）
let cleaned = false;
function cleanup() {
  if (cleaned) return;
  cleaned = true;
  if (backend.pid) {
    try {
      if (process.platform === "win32") {
        spawnSync("taskkill", ["/pid", String(backend.pid), "/T", "/F"], {
          stdio: "ignore",
        });
      } else {
        backend.kill("SIGTERM");
      }
    } catch {
      // 后端已退出则忽略
    }
  }
}

// 前端退出 → 清理后端并以相同码退出
frontend.on("exit", (code) => {
  cleanup();
  process.exit(code ?? 0);
});

// 后端异常退出 → 提示并整体退出
backend.on("exit", (code, signal) => {
  if (!cleaned) {
    console.error(`[dev] Python 后端已退出（code=${code}, signal=${signal}）`);
    cleanup();
    frontend?.kill("SIGTERM");
    process.exit(code ?? 1);
  }
});

// Ctrl+C / 终止信号 → 先清理后端再退出
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    cleanup();
    frontend.kill(sig);
    process.exit(0);
  });
}
