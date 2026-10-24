// A small JSON Schema checker covering exactly the keywords
// schema/config.schema.json uses. The bot validates every import properly with
// the jsonschema package; this only exists so the page can point at a mistake
// before the admin downloads the file, without loading a validator library.

function resolve(root, node) {
  while (node && node.$ref) {
    const target = node.$ref
      .replace(/^#\//, "")
      .split("/")
      .reduce((acc, key) => acc[key], root);
    const { $ref, ...rest } = node;
    node = { ...target, ...rest };
  }
  return node;
}

function typeOf(value) {
  if (Array.isArray(value)) return "array";
  if (value === null) return "null";
  if (Number.isInteger(value)) return "integer";
  return typeof value;
}

function typeMatches(expected, value) {
  const actual = typeOf(value);
  if (expected === "number") return actual === "integer" || actual === "number";
  return expected === actual;
}

export function validate(root, value, node = root, path = "") {
  node = resolve(root, node);
  const errors = [];
  const at = path || "(top level)";

  if (node.const !== undefined && value !== node.const) {
    errors.push(`${at}: must be ${JSON.stringify(node.const)}`);
  }
  if (node.enum && !node.enum.includes(value)) {
    errors.push(`${at}: must be one of ${node.enum.join(", ")}`);
  }
  if (node.type && !typeMatches(node.type, value)) {
    errors.push(`${at}: must be ${node.type}`);
    return errors;
  }
  if (typeof value === "string") {
    if (node.pattern && !new RegExp(node.pattern).test(value)) {
      errors.push(`${at}: "${value}" isn't in the expected format`);
    }
    if (node.maxLength !== undefined && value.length > node.maxLength) {
      errors.push(`${at}: longer than ${node.maxLength} characters`);
    }
  }
  if (typeof value === "number") {
    if (node.minimum !== undefined && value < node.minimum) errors.push(`${at}: must be at least ${node.minimum}`);
    if (node.maximum !== undefined && value > node.maximum) errors.push(`${at}: must be at most ${node.maximum}`);
  }
  if (Array.isArray(value)) {
    if (node.maxItems !== undefined && value.length > node.maxItems) {
      errors.push(`${at}: at most ${node.maxItems} items`);
    }
    if (node.uniqueItems && new Set(value.map((v) => JSON.stringify(v))).size !== value.length) {
      errors.push(`${at}: has duplicates`);
    }
    if (node.items) {
      value.forEach((item, i) => errors.push(...validate(root, item, node.items, `${path}[${i}]`)));
    }
  }
  if (typeOf(value) === "object") {
    for (const key of node.required || []) {
      if (!(key in value)) errors.push(`${at}: missing ${key}`);
    }
    const props = node.properties || {};
    for (const [key, child] of Object.entries(value)) {
      const childPath = path ? `${path}.${key}` : key;
      if (props[key]) {
        errors.push(...validate(root, child, props[key], childPath));
      } else if (node.additionalProperties === false) {
        errors.push(`${childPath}: not a known setting`);
      }
    }
  }
  return errors;
}
