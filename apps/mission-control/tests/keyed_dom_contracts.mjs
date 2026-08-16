import assert from 'node:assert/strict';
import { createKeyedReconciler } from '../frontend/dist/pure/keyed-dom.js';

class FakeNode {
  constructor(id) {
    this.id = id;
    this.parentNode = null;
    this.children = [];
    this.isConnected = true;
    this.updates = 0;
    this.animations = 0;
  }
  append(node) {
    if (node.parentNode === this) this.children = this.children.filter((item) => item !== node);
    else if (node.parentNode) node.remove();
    node.parentNode = this;
    this.children.push(node);
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((item) => item !== this);
    this.parentNode = null;
    this.isConnected = false;
  }
  getBoundingClientRect() { return { top: this.parentNode?.children.indexOf(this) * 20, left: 0 }; }
  animate() { this.animations += 1; }
}

const container = new FakeNode('container');
const keyed = createKeyedReconciler({
  container,
  key: (item) => item.id,
  create: (item) => new FakeNode(item.id),
  update: (node, item) => { node.value = item.value; node.updates += 1; },
  reducedMotion: () => false,
});

keyed.reconcile([{ id: 'a', value: 1 }, { id: 'b', value: 2 }]);
const firstA = keyed.get('a');
const firstB = keyed.get('b');
keyed.reconcile([{ id: 'b', value: 3 }, { id: 'a', value: 1 }]);
assert.equal(keyed.get('a'), firstA);
assert.equal(keyed.get('b'), firstB);
assert.deepEqual(container.children.map((node) => node.id), ['b', 'a']);
assert.equal(firstB.value, 3);
assert.equal(firstA.animations > 0 || firstB.animations > 0, true);

keyed.reconcile([{ id: 'b', value: 4 }]);
assert.equal(firstA.parentNode, null);
assert.equal(keyed.size, 1);
assert.throws(() => keyed.reconcile([{ id: 'b' }, { id: 'b' }]), /duplicate keyed DOM id/);

console.log('KEYED_DOM_CONTRACTS=PASS');
