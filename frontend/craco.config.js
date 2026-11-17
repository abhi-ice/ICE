// craco.config.js
// Plain CRA + craco setup. The old Emergent-only pieces (the dev-server
// health-check plugin and the @emergentbase/visual-edits wrapper) were
// removed for the Railway move; neither was used by the production build.
const path = require("path");

const webpackConfig = {
  eslint: {
    configure: {
      extends: ["plugin:react-hooks/recommended"],
      rules: {
        "react-hooks/rules-of-hooks": "error",
        "react-hooks/exhaustive-deps": "warn",
      },
    },
  },
  webpack: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
    configure: (webpackConfig) => {
      // Skip folders the dev server never needs to watch.
      webpackConfig.watchOptions = {
        ...webpackConfig.watchOptions,
        ignored: [
          '**/node_modules/**',
          '**/.git/**',
          '**/build/**',
          '**/dist/**',
          '**/coverage/**',
          '**/public/**',
        ],
      };
      return webpackConfig;
    },
  },
};

module.exports = webpackConfig;
