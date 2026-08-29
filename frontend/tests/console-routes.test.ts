/**
 * Every route the console calls is a route the gateway allows.
 *
 * The gateway is an allowlist, and adding a call in a component is a separate
 * edit from listing it here. Nothing connected the two, so a call to an
 * unlisted route compiled, type-checked, passed every other test, and failed
 * only in a browser against a running backend -- as a flat `no such route`
 * with nothing to say which call produced it.
 *
 * That is exactly how `POST /incidents/{id}/intake` shipped: the 911 transcript
 * used to ride along with the incident open, so the route existed on the
 * backend but had never been called from a browser and had never been listed.
 * Splitting the two calls apart meant every dispatch ended in `The call was not
 * read: no such route`.
 *
 * So this reads the console's own source, pulls out the literal paths it asks
 * for, and puts each one through the real allowlist.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { gatewayTargetPath, type GatewayMethod } from '@/lib/api/gateway-allowlist';

const ROOT = join(__dirname, '..');

function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      found.push(...sourceFiles(full));
    } else if (full.endsWith('.tsx') || full.endsWith('.ts')) {
      found.push(full);
    }
  }
  return found;
}

/**
 * A path as the allowlist will see it, with every interpolation filled in.
 *
 * The console builds these as template literals -- `/api/v1/incidents/${id}` --
 * and what the allowlist matches is the concrete path. Substituting a plausible
 * id is what turns the source's shape back into that. Ids here are deliberately
 * ordinary: the allowlist's own `ID` pattern is what decides whether a real one
 * would pass, and this test is about the *route*, not the id format.
 */
function concrete(template: string): string {
  return template
    .replace(/\$\{[^}]*districtId[^}]*\}/g, 'sffd-district-03')
    .replace(/\$\{[^}]*ncident[^}]*\}/g, 'inc_9f2c41')
    .replace(/\$\{[^}]*ddress[^}]*\}/g, 'sf-0450-hayes')
    .replace(/\$\{[^}]*\}/g, 'x1');
}

/** Every `browserGet`/`browserPost` call site, as (method, path). */
function calledRoutes(): Array<{ method: GatewayMethod; path: string; file: string }> {
  const calls: Array<{ method: GatewayMethod; path: string; file: string }> = [];
  for (const dir of ['components', 'lib', 'app']) {
    for (const file of sourceFiles(join(ROOT, dir))) {
      const source = readFileSync(file, 'utf8');
      const pattern = /browser(Get|Post)\s*<[^>]*>\s*\(\s*([`'"])([^`'"]+)\2/g;
      let match: RegExpExecArray | null;
      while ((match = pattern.exec(source)) !== null) {
        const raw = match[3]!;
        if (!raw.startsWith('/api/')) continue;
        calls.push({
          method: match[1] === 'Get' ? 'GET' : 'POST',
          // The query string is not part of what the allowlist matches.
          path: concrete(raw).split('?')[0]!,
          file: file.slice(ROOT.length + 1),
        });
      }
    }
  }
  return calls;
}

describe('the console only calls routes the gateway allows', () => {
  it('finds the console’s calls at all', () => {
    // A regex that silently matched nothing would make every assertion below
    // vacuously true, which is the one way this test could lie.
    const calls = calledRoutes();
    expect(calls.length).toBeGreaterThan(6);
    expect(calls.some((c) => c.path === '/api/v1/incidents')).toBe(true);
  });

  it('allows every path the console asks for, by its own method', () => {
    const refused = calledRoutes().filter(
      ({ method, path }) => gatewayTargetPath(path.replace(/^\//, '').split('/'), method) === null,
    );
    expect(refused).toEqual([]);
  });

  it('allows the 911 transcript, which is the one that shipped unlisted', () => {
    expect(
      gatewayTargetPath('api/v1/incidents/inc_9f2c41/intake'.split('/'), 'POST'),
    ).not.toBeNull();
    // And not by accident of a loose pattern: it is a write.
    expect(gatewayTargetPath('api/v1/incidents/inc_9f2c41/intake'.split('/'), 'GET')).toBeNull();
  });
});
