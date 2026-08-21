import assert from 'node:assert/strict';
import { createRetainedRoutes } from '../frontend/dist/pure/retained-routes.js';

class FakeNode {
  constructor(id = '') {
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.hidden = false;
    this.inert = false;
    this.attrs = new Map();
    this.isConnected = true;
    this.focusCount = 0;
  }
  append(node) {
    if (node.parentNode) node.remove();
    node.parentNode = this;
    this.children.push(node);
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((node) => node !== this);
    this.parentNode = null;
    this.isConnected = false;
  }
  contains(node) {
    return node === this || this.children.some((child) => child.contains?.(node));
  }
  setAttribute(key, value) { this.attrs.set(key, String(value)); }
  removeAttribute(key) { this.attrs.delete(key); }
  focus() { this.focusCount += 1; }
}

const container = new FakeNode('container');
let focused = null;
const deferred = [];
const evicted = [];
const retained = createRetainedRoutes({
  container,
  limit: 2,
  createRoot: (id) => new FakeNode(id),
  activeElement: () => focused,
  defer: (fn) => deferred.push(fn),
  onEvict: (id) => evicted.push(id),
});

const overview = retained.ensure('default::overview');
const overviewInput = new FakeNode('overview-input');
overview.append(overviewInput);
retained.activate('default::overview');
focused = overviewInput;

const issues = retained.ensure('default::issues');
retained.activate('default::issues');
assert.equal(overview.hidden, true);
assert.equal(overview.inert, true);
assert.equal(issues.hidden, false);
assert.equal(issues.inert, false);

// Returning to a route reuses the exact root and restores its focused node.
assert.equal(retained.activate('default::overview').root, overview);
for (const fn of deferred.splice(0)) fn();
assert.equal(overviewInput.focusCount, 1);

// LRU is bounded, evicts only an inactive route, and leaves active DOM intact.
const kanban = retained.ensure('default::kanban');
retained.activate('default::kanban');
assert.deepEqual(retained.ids().sort(), ['default::kanban', 'default::overview']);
assert.deepEqual(evicted, ['default::issues']);
assert.equal(overview.isConnected, true);
assert.equal(kanban.isConnected, true);

console.log('RETAINED_DOM_CONTRACTS=PASS');
