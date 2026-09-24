// Rule 15 — Conventional commit messages
// Install: npm install --save-dev @commitlint/cli @commitlint/config-conventional
// Then add to package.json: "husky": { "hooks": { "commit-msg": "commitlint -E HUSKY_GIT_PARAMS" } }

module.exports = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    // Enforce type
    'type-enum': [2, 'always', [
      'feat',      // new feature
      'fix',       // bug fix
      'docs',      // documentation only
      'style',     // formatting (no logic change)
      'refactor',  // code change (no feature/fix)
      'test',      // adding/updating tests (Rule 13)
      'chore',     // tooling, config, deps
      'ci',        // CI/CD changes
      'build',     // build system changes
      'perf',      // performance improvement (Rule 9)
      'revert',    // revert a commit
      'security',  // security fix (Rule 5)
    ]],

    // Subject line
    'subject-case': [2, 'never', ['start-case', 'pascal-case', 'upper-case']],
    'subject-max-length': [2, 'always', 100],
    'subject-empty': [2, 'never'],

    // Body
    'body-max-line-length': [2, 'always', 200],

    // Scope (optional but recommended)
    'scope-case': [2, 'always', 'lower-case'],
  },

  // Custom prompt for git commit interactive mode
  prompt: {
    questions: {
      type: {
        description: 'Select the type of change (Rule 15)',
        enum: {
          feat:     { description: 'A new feature', title: 'Features', emoji: '✨' },
          fix:      { description: 'A bug fix', title: 'Bug Fixes', emoji: '🐛' },
          docs:     { description: 'Documentation only changes', title: 'Documentation', emoji: '📚' },
          style:    { description: 'Formatting, whitespace (no logic)', title: 'Styles', emoji: '💎' },
          refactor: { description: 'Code change, no feature or fix', title: 'Code Refactoring', emoji: '📦' },
          test:     { description: 'Add or update tests', title: 'Tests', emoji: '🚨' },
          chore:    { description: 'Tooling, config, deps', title: 'Chores', emoji: '♻️' },
          security: { description: 'Security fix', title: 'Security', emoji: '🔒' },
        },
      },
    },
  },
};
