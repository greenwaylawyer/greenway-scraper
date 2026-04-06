"""Browser stealth patches for Cloudflare / bot-detection bypass.

Applies JavaScript init scripts to a Playwright page that hide the most common
automation fingerprinting signals:
  - navigator.webdriver = undefined
  - window.chrome runtime object present
  - Realistic navigator.plugins and navigator.languages
  - Permissions API normalised (Cloudflare probes this)
  - WebGL vendor/renderer strings spoofed

Usage:
    from utils.stealth import stealth_async
    await stealth_async(page)

This replaces the unmaintained playwright-stealth package which has a broken
pkg_resources dependency on Python 3.12+.
"""

from playwright.async_api import Page

_STEALTH_SCRIPT = """
// 1. Hide webdriver flag
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// 2. Add chrome runtime (missing in headless)
window.chrome = {
    runtime: {
        PlatformOs: {MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd'},
        PlatformArch: {ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64'},
        PlatformNaclArch: {ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64'},
        RequestUpdateCheckStatus: {THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available'},
        OnInstalledReason: {INSTALL: 'install', UPDATE: 'update', CHROME_UPDATE: 'chrome_update', SHARED_MODULE_UPDATE: 'shared_module_update'},
        OnRestartRequiredReason: {APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic'},
    },
    loadTimes: function() {},
    csi: function() {},
    app: {},
};

// 3. Realistic plugins list
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const makePlugin = (name, filename, mimeTypes) => {
            const p = {name, filename, description: '', length: mimeTypes.length};
            mimeTypes.forEach((mt, i) => { p[i] = mt; });
            p[Symbol.iterator] = Array.prototype[Symbol.iterator].bind(Object.values(mimeTypes));
            return p;
        };
        return [
            makePlugin('Chrome PDF Plugin', 'internal-pdf-viewer', [
                {type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format'}
            ]),
            makePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', [
                {type: 'application/pdf', suffixes: 'pdf', description: ''}
            ]),
            makePlugin('Native Client', 'internal-nacl-plugin', [
                {type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable'},
                {type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable'},
            ]),
        ];
    },
});

// 4. Realistic languages
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

// 5. Permissions API – Cloudflare probes notification permission
const _origPermQuery = window.navigator.permissions.query.bind(navigator.permissions);
window.navigator.permissions.query = (params) => {
    if (params.name === 'notifications') {
        return Promise.resolve({state: Notification.permission, onchange: null});
    }
    return _origPermQuery(params);
};

// 6. WebGL vendor/renderer
const _origGetParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';          // UNMASKED_VENDOR_WEBGL
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';  // UNMASKED_RENDERER_WEBGL
    return _origGetParam.call(this, parameter);
};

// 7. Hide automation-related properties
delete navigator.__proto__.webdriver;
"""


async def stealth_async(page: Page) -> None:
    """Apply stealth patches to a Playwright page before any navigation."""
    await page.add_init_script(_STEALTH_SCRIPT)
