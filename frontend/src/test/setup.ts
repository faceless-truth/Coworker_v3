import '@testing-library/jest-dom/vitest'

// jsdom doesn't implement scrollIntoView; the Chat page calls it on
// every streaming update. Stub once globally so tests don't crash.
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {}
}
