"""PyInstaller 打包脚本 - 把远程桌面打包成单个 exe。

用法:
    python build.py

生成:
    dist/远程桌面.exe  （双击即可运行）

注意事项:
    1. 先运行 python run.py --install-only 安装所有依赖
    2. 再运行 pip install pyinstaller
    3. 最后运行 python build.py
"""
import subprocess
import sys
import os
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def main():
    print("=" * 56)
    print("  远程桌面 - 打包工具")
    print("=" * 56)

    # 检查 pyinstaller
    try:
        import PyInstaller
        print("[1/4] PyInstaller 已安装")
    except ImportError:
        print("[1/4] 安装 PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # 清理旧文件
    build_dir = os.path.join(BASE_DIR, "build")
    dist_dir = os.path.join(BASE_DIR, "dist")
    spec_file = os.path.join(BASE_DIR, "远程桌面.spec")
    for p in [build_dir, dist_dir, spec_file]:
        if os.path.exists(p):
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            print(f"[2/4] 清理: {os.path.basename(p)}")

    # PyInstaller 命令
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "远程桌面",
        "--console",
        # 添加数据文件
        "--add-data", f"templates{os.pathsep}templates",
        "--add-data", f"static{os.pathsep}static",
        # 添加隐藏导入
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "uvicorn.lifespan.off",
        "--hidden-import", "jinja2",
        "--hidden-import", "mss",
        "--hidden-import", "PIL",
        "--hidden-import", "pyautogui",
        "--hidden-import", "pyperclip",
        "--hidden-import", "soundcard",
        "--hidden-import", "numpy",
        # 图标（如果有）
        # "--icon", "icon.ico",
        # 入口文件
        "run.py",
    ]

    print("[3/4] 开始打包（可能需要几分钟）...")
    print(f"  命令: {' '.join(cmd[:5])}...")

    result = subprocess.run(cmd, cwd=BASE_DIR)

    if result.returncode != 0:
        print("\n[4/4] 打包失败!")
        return False

    exe_path = os.path.join(dist_dir, "远程桌面.exe")
    if os.path.exists(exe_path):
        size_mb = os.path.getsize(exe_path) / 1024 / 1024
        print(f"\n[4/4] 打包成功!")
        print(f"  文件: {exe_path}")
        print(f"  大小: {size_mb:.1f} MB")
        print(f"\n  双击运行即可，会自动:")
        print(f"  1. 检查并安装依赖")
        print(f"  2. 启动服务")
        print(f"  3. 打开浏览器")
        return True
    else:
        print("\n[4/4] 打包可能失败，未找到 exe 文件")
        return False


if __name__ == "__main__":
    main()
    input("\n按回车键退出...")
