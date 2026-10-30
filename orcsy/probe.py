"""静态探测项目的技术栈与常用命令，用于自动填充 AGENT.md 的 Project 节。

只读 manifest 文件（package.json / pyproject.toml / Cargo.toml / go.mod 等），
不执行任何命令、不扫描源码内容，零 token、确定性强。探测不到就留空，
由开发者手填——宁可留白也不瞎猜。
"""

from __future__ import annotations

import json
from pathlib import Path

# (manifest 依赖 key, 展示名)：按 key 子串命中依赖列表即认为用了该框架
_NODE_FRAMEWORKS = [
    ("vue", "Vue"), ("react", "React"), ("next", "Next.js"), ("nuxt", "Nuxt"),
    ("svelte", "Svelte"), ("express", "Express"), ("nest", "NestJS"),
    ("vite", "Vite"), ("electron", "Electron"), ("naive-ui", "Naive UI"),
]
_PY_FRAMEWORKS = [
    ("fastapi", "FastAPI"), ("flask", "Flask"), ("django", "Django"),
    ("pyqt", "PyQt"), ("pyside", "PySide"), ("typer", "Typer"), ("click", "Click"),
]


def probe_project(path: Path) -> dict[str, str]:
    """返回 {"stack", "run", "test"} 三个字段；探测不到的值为空串。"""
    stack: list[str] = []
    run: list[str] = []
    test: list[str] = []
    for prober in (_probe_node, _probe_python, _probe_rust, _probe_go,
                   _probe_dotnet, _probe_java, _probe_cmake):
        prober(path, stack, run, test)
    return {"stack": "; ".join(stack), "run": " / ".join(run), "test": " / ".join(test)}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _probe_node(path: Path, stack: list[str], run: list[str], test: list[str]) -> None:
    if not (path / "package.json").is_file():
        return
    try:
        data = json.loads(_read(path / "package.json"))
    except ValueError:
        data = {}
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    dep_keys = {k.lower() for k in deps}
    frameworks = [fw for key, fw in _NODE_FRAMEWORKS
                  if any(key in k for k in dep_keys)]
    scripts = data.get("scripts") or {}
    if (path / "pnpm-lock.yaml").is_file():
        pm = "pnpm"
    elif (path / "yarn.lock").is_file():
        pm = "yarn"
    else:
        pm = "npm"
    stack.append("Node.js" + (f" ({', '.join(frameworks)})" if frameworks else ""))
    if "test" in scripts:
        test.append(f"{pm} test")
    if "dev" in scripts:
        run.append(f"{pm} run dev")
    elif "start" in scripts:
        run.append(f"{pm} start")


def _probe_python(path: Path, stack: list[str], run: list[str], test: list[str]) -> None:
    manifests = [p for p in (path / "pyproject.toml", path / "requirements.txt",
                             path / "setup.py", path / "uv.lock") if p.is_file()]
    if not manifests:
        return
    text = "\n".join(_read(p).lower() for p in manifests)
    frameworks = [fw for key, fw in _PY_FRAMEWORKS if key in text]
    stack.append("Python" + (f" ({', '.join(frameworks)})" if frameworks else ""))
    if "pytest" in text or (path / "tests").is_dir():
        test.append("pytest")
    else:
        test.append("python -m unittest discover")
    if "django" in text and (path / "manage.py").is_file():
        run.append("python manage.py runserver")
    elif (path / "uv.lock").is_file():
        run.append("uv run <entry>")


def _probe_rust(path: Path, stack: list[str], run: list[str], test: list[str]) -> None:
    if not (path / "Cargo.toml").is_file():
        return
    stack.append("Rust")
    run.append("cargo run")
    test.append("cargo test")


def _probe_go(path: Path, stack: list[str], run: list[str], test: list[str]) -> None:
    if not (path / "go.mod").is_file():
        return
    stack.append("Go")
    run.append("go run .")
    test.append("go test ./...")


def _probe_dotnet(path: Path, stack: list[str], run: list[str], test: list[str]) -> None:
    try:
        has_csproj = any(path.glob("*.csproj")) or any(path.glob("*.sln"))
    except OSError:
        has_csproj = False
    if not has_csproj:
        return
    stack.append(".NET")
    test.append("dotnet test")
    if any(path.glob("*.csproj")):
        run.append("dotnet run")
    else:
        run.append("dotnet build")


def _probe_java(path: Path, stack: list[str], run: list[str], test: list[str]) -> None:
    if (path / "pom.xml").is_file():
        stack.append("Java (Maven)")
        test.append("mvn test")
    elif (path / "build.gradle").is_file() or (path / "build.gradle.kts").is_file():
        stack.append("Java (Gradle)")
        test.append("gradle test")


def _probe_cmake(path: Path, stack: list[str], run: list[str], test: list[str]) -> None:
    if not (path / "CMakeLists.txt").is_file():
        return
    stack.append("C/C++ (CMake)")
    test.append("ctest")
    # C++ 项目入口不固定，run 留空
