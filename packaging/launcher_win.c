// Windows launcher for OpenKimo (and white-label rebrands).
//
// Replaces the PyInstaller onefile launcher with a small native exe.
//
// Why: the onefile bootloader extracted the bundled python312.dll into
// %TEMP% at the moment the installer's "Launch app" fired, and an AV
// real-time scanner (e.g. Tencent PC Manager) that was still scanning the
// freshly-written DLL would lock it — producing "Failed to load Python
// DLL". A native launcher bundles no Python DLL and extracts nothing at
// launch, so there is simply nothing for the scanner to lock.
//
// What it does (mirrors the old launcher.py logic):
//   1. Resolve the install root = directory containing this exe ({app}).
//   2. Spawn <root>\runtime\python\pythonw.exe -m packaging.app_main_win
//      (falling back to python.exe if pythonw.exe is absent), with the
//      same PYTHONPATH / env vars the Python launcher set.
//   3. DETACHED_PROCESS so this exe returns immediately; the tray
//      supervisor lives in the spawned pythonw process.
//
// build_windows.py compiles this with MinGW-w64 gcc:
//   gcc -O2 -municode -mwindows -o OpenKimo.exe launcher_win.c -ladvapi32
//
// -municode selects wWinMain so we get a UTF-16 command line / env.
// -mwindows links the GUI subsystem (no console window).

#include <windows.h>
#include <wchar.h>
#include <stdlib.h>
#include <string.h>

// Join `dir` + "\\" + `rel`; returns malloc'd string (caller frees).
static wchar_t *join_path(const wchar_t *dir, const wchar_t *rel) {
    size_t n = wcslen(dir) + 1 + wcslen(rel) + 1;
    wchar_t *out = (wchar_t *)malloc(n * sizeof(wchar_t));
    if (!out) return NULL;
    wcscpy(out, dir);
    wcscat(out, L"\\");
    wcscat(out, rel);
    return out;
}

// Append `src` to *dest (which may be NULL) separated by ';'.
// Replaces *dest with a new buffer (caller must eventually free it).
static void append_path(wchar_t **dest, const wchar_t *src) {
    if (!src || !*src) return;
    size_t dn = (*dest) ? wcslen(*dest) : 0;
    size_t sn = wcslen(src);
    wchar_t *out = (wchar_t *)malloc((dn + 1 + sn + 1) * sizeof(wchar_t));
    if (!out) return;
    if (dn) {
        wcscpy(out, *dest);
        wcscat(out, L";");
        wcscat(out, src);
    } else {
        wcscpy(out, src);
    }
    free(*dest);
    *dest = out;
}

static int file_exists(const wchar_t *path) {
    DWORD a = GetFileAttributesW(path);
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

// Append `line` (UTF-16) as UTF-8 to <root>\launcher-error.log.
static void log_error(const wchar_t *root, const wchar_t *line) {
    wchar_t *log = join_path(root, L"launcher-error.log");
    if (!log) return;
    HANDLE h = CreateFileW(log, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    if (h != INVALID_HANDLE_VALUE) {
        int len = WideCharToMultiByte(CP_UTF8, 0, line, -1, NULL, 0, NULL, NULL);
        if (len > 1) {
            char *utf8 = (char *)malloc(len);
            if (utf8) {
                WideCharToMultiByte(CP_UTF8, 0, line, -1, utf8, len, NULL, NULL);
                DWORD written;
                WriteFile(h, utf8, (DWORD)(len - 1), &written, NULL);
                free(utf8);
            }
        }
        CloseHandle(h);
    }
    free(log);
}

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, PWSTR pCmdLine, int nShow) {
    (void)hInst; (void)hPrev; (void)pCmdLine; (void)nShow;

    // 1. Install root = directory of this exe.
    wchar_t exe[MAX_PATH];
    if (!GetModuleFileNameW(NULL, exe, MAX_PATH)) return 1;
    wchar_t *slash = wcsrchr(exe, L'\\');
    if (!slash) return 1;
    *slash = L'\0';                       // exe now holds {app}
    wchar_t root[MAX_PATH];
    wcscpy(root, exe);

    // 2. runtime\python\pythonw.exe, fallback python.exe.
    wchar_t *runtime = join_path(root, L"runtime");
    wchar_t *pydir = join_path(runtime, L"python");
    wchar_t *python = join_path(pydir, L"pythonw.exe");
    if (!file_exists(python)) {
        free(python);
        python = join_path(pydir, L"python.exe");
    }
    if (!file_exists(python)) {
        wchar_t msg[1024];
        swprintf(msg, 1024, L"runtime python not found under %s\n", runtime);
        log_error(root, msg);
        return 1;
    }

    // 3. PYTHONPATH (same order as the Python launcher):
    //    runtime; site-packages; site-packages\win32;
    //    site-packages\win32\lib; site-packages\Pythonwin; + existing.
    wchar_t *sp = join_path(runtime, L"site-packages");
    wchar_t *win32 = join_path(sp, L"win32");
    wchar_t *win32lib = join_path(win32, L"lib");
    wchar_t *pythonwin = join_path(sp, L"Pythonwin");

    wchar_t *pp = NULL;
    append_path(&pp, runtime);
    append_path(&pp, sp);
    append_path(&pp, win32);
    append_path(&pp, win32lib);
    append_path(&pp, pythonwin);
    wchar_t existing[MAX_PATH * 8];
    DWORD en = GetEnvironmentVariableW(L"PYTHONPATH", existing, MAX_PATH * 8);
    if (en > 0 && en < MAX_PATH * 8) append_path(&pp, existing);

    // 4. Env for the child (inherited because we pass NULL below).
    SetEnvironmentVariableW(L"PYTHONPATH", pp);
    SetEnvironmentVariableW(L"KIMO_OPENKIMO_WIN", L"1");
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");

    // 5. Command line: "<python>" -m packaging.app_main_win
    wchar_t cmd[1024];
    wcscpy(cmd, L"\"");
    wcscat(cmd, python);
    wcscat(cmd, L"\" -m packaging.app_main_win");

    // 6. Detached spawn; this exe returns immediately.
    STARTUPINFOW si;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi;
    ZeroMemory(&pi, sizeof(pi));
    DWORD flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP;
    BOOL ok = CreateProcessW(python, cmd, NULL, NULL, FALSE, flags,
                             NULL, root, &si, &pi);
    if (!ok) {
        wchar_t msg[1024];
        swprintf(msg, 1024, L"CreateProcess failed: %lu\n", GetLastError());
        log_error(root, msg);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);

    free(pythonwin);
    free(win32lib);
    free(win32);
    free(sp);
    free(pp);
    free(python);
    free(pydir);
    free(runtime);
    return 0;
}
