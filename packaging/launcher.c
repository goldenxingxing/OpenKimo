// macOS .app launcher for OpenKimo (and white-label rebrands).
//
// Why a C binary instead of a shell script or Frameworks/.../bin/python3
// directly as CFBundleExecutable:
//   On macOS Sonoma+ a subprocess-spawned Python is treated as "NotVisible"
//   by ControlCenter and the menu bar status item gets hidden. The
//   foreground process must dlopen libpython in-process and call
//   Py_BytesMain so that NSStatusItem creation happens in the launched app.
//
// venvstacks layout assumed at runtime (layer name prefixes are fixed by
// venvstacks; the suffix is whatever was declared in venvstacks.toml, so
// we discover them at runtime by scanning Resources/runtimes/):
//   <bundle>/Contents/Resources/runtimes/cpython-<ver>/                  (runtime)
//   <bundle>/Contents/Resources/runtimes/framework-<name>/               (framework)
//   <bundle>/Contents/Resources/runtimes/app-<name>/                     (application)
//
// To get a layered Python with all three site-packages on sys.path we:
//   1. Build DYLD_LIBRARY_PATH so .so extensions can find lower-layer dylibs
//      (mirrors what the app-layer bin/python wrapper script does).
//   2. dlopen the cpython layer's libpython (only one in the bundle).
//   3. Call Py_BytesMain with argv[0] = app-layer bin/python — Python uses
//      that path to locate pyvenv.cfg and load sitecustomize.py, which in
//      turn appends every layer's site-packages to sys.path.
//
// build.py compiles this with:
//   cc -arch <arch> -mmacosx-version-min=14.0 -O2 \
//      -DCPYTHON_PREFIX='"cpython-3.12"' \
//      -o "<bundle>/Contents/MacOS/<AppName>" launcher.c

#include <dirent.h>
#include <dlfcn.h>
#include <libgen.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/param.h>
#include <unistd.h>
#include <mach-o/dyld.h>

#ifndef CPYTHON_PREFIX
#define CPYTHON_PREFIX "cpython-3.12"
#endif

typedef int (*Py_BytesMain_t)(int argc, char **argv);

// Resolve `<runtimes>` to its absolute path based on the running binary.
// Returns malloc'd string (caller frees), or NULL on error.
static char *resolve_runtimes_dir(void) {
    char exe_path[MAXPATHLEN];
    uint32_t size = sizeof(exe_path);
    if (_NSGetExecutablePath(exe_path, &size) != 0) {
        fprintf(stderr, "launcher: _NSGetExecutablePath failed\n");
        return NULL;
    }
    char real_exe[MAXPATHLEN];
    if (realpath(exe_path, real_exe) == NULL) {
        strncpy(real_exe, exe_path, MAXPATHLEN);
    }
    char dir_buf[MAXPATHLEN];
    strncpy(dir_buf, real_exe, MAXPATHLEN);
    char *dir = dirname(dir_buf);   // .../Contents/MacOS

    char *out = malloc(MAXPATHLEN);
    if (!out) return NULL;
    snprintf(out, MAXPATHLEN, "%s/../Resources/runtimes", dir);
    return out;
}

// Find the first child of `dir` whose name starts with `prefix`. Returns
// malloc'd absolute path (caller frees), or NULL if not found.
static char *find_layer(const char *runtimes_dir, const char *prefix) {
    DIR *d = opendir(runtimes_dir);
    if (!d) return NULL;
    char *out = NULL;
    struct dirent *ent;
    size_t prefix_len = strlen(prefix);
    while ((ent = readdir(d)) != NULL) {
        if (ent->d_name[0] == '.') continue;
        if (strncmp(ent->d_name, prefix, prefix_len) == 0) {
            size_t need = strlen(runtimes_dir) + 1 + strlen(ent->d_name) + 1;
            out = malloc(need);
            if (out) snprintf(out, need, "%s/%s", runtimes_dir, ent->d_name);
            break;
        }
    }
    closedir(d);
    return out;
}

// Append `entry` to colon-separated env var `name`, creating it if absent.
static void append_env(const char *name, const char *entry) {
    const char *cur = getenv(name);
    if (!cur || !*cur) {
        setenv(name, entry, 1);
        return;
    }
    size_t need = strlen(cur) + 1 + strlen(entry) + 1;
    char *combined = malloc(need);
    if (!combined) return;
    snprintf(combined, need, "%s:%s", cur, entry);
    setenv(name, combined, 1);
    free(combined);
}

// Prepend `entry` to colon-separated env var `name`. Used for PYTHONPATH so
// bundle paths win over a shell-inherited PYTHONPATH (e.g. dev machine where
// the source tree is on PYTHONPATH and would otherwise shadow bundled
// `app_main`).
static void prepend_env(const char *name, const char *entry) {
    const char *cur = getenv(name);
    if (!cur || !*cur) {
        setenv(name, entry, 1);
        return;
    }
    size_t need = strlen(entry) + 1 + strlen(cur) + 1;
    char *combined = malloc(need);
    if (!combined) return;
    snprintf(combined, need, "%s:%s", entry, cur);
    setenv(name, combined, 1);
    free(combined);
}

int main(int argc, char *argv[]) {
    char *runtimes = resolve_runtimes_dir();
    if (!runtimes) return 1;

    char *cpython_layer  = find_layer(runtimes, "cpython-");
    char *framework_layer = find_layer(runtimes, "framework-");
    char *app_layer      = find_layer(runtimes, "app-");
    if (!cpython_layer || !app_layer) {
        fprintf(stderr, "launcher: missing venvstacks layers in %s\n", runtimes);
        return 1;
    }

    // 1. DYLD_LIBRARY_PATH — let extension .so find lower-layer dylibs.
    if (framework_layer) {
        char buf[MAXPATHLEN];
        snprintf(buf, sizeof(buf), "%s/share/venv/dynlib", framework_layer);
        append_env("DYLD_LIBRARY_PATH", buf);
    }
    {
        char buf[MAXPATHLEN];
        snprintf(buf, sizeof(buf), "%s/share/venv/dynlib", cpython_layer);
        append_env("DYLD_LIBRARY_PATH", buf);
    }

    // 2. dlopen cpython's libpython.
    char dylib_path[MAXPATHLEN];
    snprintf(dylib_path, sizeof(dylib_path),
             "%s/lib/libpython3.12.dylib", cpython_layer);
    void *handle = dlopen(dylib_path, RTLD_NOW | RTLD_GLOBAL);
    if (!handle) {
        fprintf(stderr, "launcher: failed to dlopen %s: %s\n",
                dylib_path, dlerror());
        return 1;
    }
    Py_BytesMain_t Py_BytesMain = (Py_BytesMain_t)dlsym(handle, "Py_BytesMain");
    if (!Py_BytesMain) {
        fprintf(stderr, "launcher: failed to resolve Py_BytesMain: %s\n",
                dlerror());
        return 1;
    }

    // 3. PYTHONPATH (final order, leftmost wins):
    //    Resources/                                 (so `python -m app_main` resolves)
    //    <app>/lib/python3.12/site-packages
    //    <framework>/lib/python3.12/site-packages
    //    <cpython>/lib/python3.12/site-packages
    //    <existing $PYTHONPATH>                     (user/shell additions)
    //
    // The site-package entries are load-bearing: venvstacks' postinstall.py
    // bakes absolute paths into each layer's sitecustomize.py at build time,
    // so once the bundle is moved (DMG → /Applications, or any other place),
    // those addsitedir() calls silently no-op and rumps/fastapi/etc. become
    // unreachable. Building PYTHONPATH from the resolved layer dirs at run
    // time makes the bundle properly relocatable.
    //
    // We *prepend* (not append) so a shell-inherited PYTHONPATH (e.g. a dev
    // machine with the source tree on PYTHONPATH) cannot shadow bundled
    // `app_main` or bundled site-packages. Prepend in reverse so the leftmost
    // entry of the final string is `Resources/`.
    char sp[MAXPATHLEN];
    snprintf(sp, sizeof(sp),
             "%s/lib/python3.12/site-packages", cpython_layer);
    prepend_env("PYTHONPATH", sp);
    if (framework_layer) {
        snprintf(sp, sizeof(sp),
                 "%s/lib/python3.12/site-packages", framework_layer);
        prepend_env("PYTHONPATH", sp);
    }
    snprintf(sp, sizeof(sp), "%s/lib/python3.12/site-packages", app_layer);
    prepend_env("PYTHONPATH", sp);

    char resources_dir[MAXPATHLEN];
    snprintf(resources_dir, sizeof(resources_dir), "%s/..", runtimes);
    prepend_env("PYTHONPATH", resources_dir);

    // 4. chdir into the bundle's Resources/ so Python's `-m` module discovery
    //    cannot accidentally pick up an `app_main/` directory in the user's
    //    shell CWD (e.g. our own source tree on a dev machine). With CWD
    //    pointing inside the bundle, sys.path[0] = bundle Resources and the
    //    bundled `app_main` always wins.
    if (chdir(resources_dir) != 0) {
        fprintf(stderr, "launcher: chdir to %s failed\n", resources_dir);
        // non-fatal: PYTHONPATH still has bundle paths first
    }

    // 5. Build argv: [<app-layer/bin/python>, "-m", "app_main", ...rest]
    char app_python[MAXPATHLEN];
    snprintf(app_python, sizeof(app_python), "%s/bin/python", app_layer);

    char **new_argv = calloc(argc + 3, sizeof(char *));
    if (!new_argv) return 1;
    new_argv[0] = app_python;
    new_argv[1] = "-m";
    new_argv[2] = "app_main";
    for (int i = 1; i < argc; i++) {
        new_argv[i + 2] = argv[i];
    }
    new_argv[argc + 2] = NULL;

    int rc = Py_BytesMain(argc + 2, new_argv);

    free(new_argv);
    free(cpython_layer);
    if (framework_layer) free(framework_layer);
    free(app_layer);
    free(runtimes);
    return rc;
}
