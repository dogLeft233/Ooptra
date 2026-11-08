import js from '@eslint/js';
import globals from 'globals';

// index.html 的普通 script 共享这些声明；只登记已核实的跨文件接口。
// 新增接口时同时核实定义文件和脚本加载顺序，拼写错误仍由 no-undef 拦截。
const sharedGlobals = {
  $: 'readonly', api: 'readonly', text: 'readonly', toast: 'readonly',
  show: 'readonly', escapeHtml: 'readonly', GROUP_TITLE: 'readonly',
  SIGNED_OUT_KEY: 'readonly', confirmDialog: 'readonly', copyText: 'readonly',
  positionToasts: 'readonly', clearLegacyCredentials: 'readonly',
  showLogin: 'readonly', refreshStatus: 'readonly', withToken: 'readonly',
  fmtClock: 'readonly', fmtDuration: 'readonly', stopPolling: 'readonly',
  stopLogStream: 'readonly', logNode: 'readonly',
  dirty: 'writable', configSchema: 'writable', showAdvanced: 'writable', state: 'writable',
  updateSavebar: 'readonly', draftCount: 'readonly', loadConfig: 'readonly',
  renderConfig: 'readonly', syncConfigChips: 'readonly', wireConfig: 'readonly',
  switchVoiceTab: 'readonly', refreshVoiceStatus: 'writable',
  _origRefreshVoiceStatus: 'readonly', refreshAutoVisit: 'readonly',
  voiceAreaNames: 'writable', loadBindings: 'readonly',
  changeVoiceOutput: 'readonly', resetVoiceOutput: 'readonly',
};

export default [
  { ignores: ['src/oopz_sdk/**', 'node_modules/**', '.browser-ci/**', 'webui_preview.html'] },
  {
    files: ['src/webui/assets/*.js', 'tests/browser/*.cjs', 'eslint.config.mjs'],
    ...js.configs.recommended,
  },
  {
    files: ['src/webui/assets/*.js'],
    languageOptions: {
      sourceType: 'script',
      globals: { ...globals.browser, ...sharedGlobals },
    },
    rules: {
      // 普通 script 在页面共享顶层声明；局部未使用变量仍报错。
      'no-unused-vars': ['error', { vars: 'local', args: 'none', caughtErrors: 'none' }],
      // 共享接口也会在其定义脚本声明；同文件重复声明仍报错。
      'no-redeclare': ['error', { builtinGlobals: false }],
    },
  },
  {
    files: ['src/webui/assets/app.js'],
    // API 层将认证失败统一转换为 AuthError，维持现有错误展示契约。
    rules: { 'preserve-caught-error': 'off' },
  },
  {
    files: ['tests/browser/*.cjs'],
    languageOptions: {
      sourceType: 'commonjs',
      // page.evaluate 回调使用浏览器全局对象，测试驱动使用 Node API。
      globals: { ...globals.node, ...globals.browser, ...sharedGlobals },
    },
    rules: { 'no-unused-vars': ['error', { args: 'none', caughtErrors: 'none' }] },
  },
  {
    files: ['src/webui/assets/voice.js'],
    // 现有代码用包装函数重绑定 refreshVoiceStatus，以附加串门刷新。
    rules: { 'no-func-assign': 'off' },
  },
  {
    files: ['tests/browser/voice-volume.cjs'],
    // 此测试在 SDK voice WebView 的 page.evaluate 中访问其内部状态。
    languageOptions: {
      globals: {
        client: 'writable', ttsCtx: 'readonly', ttsGain: 'readonly',
        ensureTtsTrack: 'readonly', teardownTtsPipeline: 'readonly',
        _currentVolume: 'readonly',
      },
    },
  },
  {
    files: ['eslint.config.mjs'],
    languageOptions: { sourceType: 'module', globals: globals.node },
  },
];
