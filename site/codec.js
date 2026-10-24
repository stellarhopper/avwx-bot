// The configurator link format, shared with bot/guildcfg.py.
//
// JSON → raw DEFLATE → base64url without padding. The browser's own
// CompressionStream does the DEFLATE, so there is no library to load. This
// module also runs under Node (tests/test_site_codec.py drives it), which has
// the same streams built in.

function toBase64Url(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(text) {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/");
  const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

async function pipe(bytes, transform) {
  const stream = new Blob([bytes]).stream().pipeThrough(transform);
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

export async function encodeFragment(doc) {
  const bytes = new TextEncoder().encode(JSON.stringify(doc));
  return toBase64Url(await pipe(bytes, new CompressionStream("deflate-raw")));
}

export async function decodeFragment(text) {
  const bytes = await pipe(fromBase64Url(text), new DecompressionStream("deflate-raw"));
  return JSON.parse(new TextDecoder().decode(bytes));
}
