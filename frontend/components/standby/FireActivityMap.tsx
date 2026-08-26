/**
 * Regional fire activity: satellite thermal detections, drawn over the bbox
 * they were queried for, with the fire weather that came back beside them.
 *
 * **The normal reading is an empty city inside a busy region, and this panel
 * exists to render that as a fact.** VIIRS pixels are roughly 375 m across and
 * the product is built for wildfire; a room-and-contents fire on Hayes Street
 * never reaches the detection threshold. Against the live API, San Francisco
 * proper returns zero detections while Northern California returns hundreds.
 * So a zero here is neither an alarm nor a failure -- it is the instrument
 * working, and the panel says so in one line rather than filling the city with
 * a marker to make the map look alive. There is deliberately no city marker to
 * invent.
 *
 * The map is inline SVG over a linear lon/lat projection of the bbox the
 * backend reports it queried. No tiles, no map library, no basemap: a basemap
 * would imply a geographic precision a 375 m pixel does not have, and the only
 * geography an officer needs from this panel is "how far from us".
 *
 * The fire weather is NASA POWER reanalysis, which runs days behind real
 * behind. It is labelled with the observation window the payload carries and
 * is never presented as current: the console surfaces live NWS wind elsewhere,
 * and two wind readings that look alike but are days apart is exactly the
 * kind of quiet substitution this codebase refuses to make.
 *
 * Nothing is inferred. Every count, every bbox and every reading is read off
 * the response; where the response carries none, the panel says "not reported"
 * and draws nothing.
 */

// --------------------------------------------------------------------- types

/** A degrees-decimal bounding box, west/south/east/north. */
export interface FireBBox {
  west: number;
  south: number;
  east: number;
  north: number;
}

/** One satellite thermal detection, as the fire-activity payload carries it. */
export interface FireDetection {
  latitude: number;
  longitude: number;
  /** VIIRS reports `l`/`n`/`h`; MODIS reports 0-100. Both are accepted. */
  confidence: string | number | null;
  /** Fire radiative power, megawatts. `null` where the product omits it. */
  frp: number | null;
  acquired_at: string | null;
  satellite: string | null;
}

/** The fire-weather block: recent reanalysis, never a current observation. */
export interface FireWeather {
  available: boolean;
  unavailable_reason: string | null;
  temperature_c: number | null;
  relative_humidity_pct: number | null;
  wind_speed_ms: number | null;
  wind_direction_deg: number | null;
  /** The window the reanalysis covers, either as two instants or as text. */
  observation_start: string | null;
  observation_end: string | null;
  observation_window: string | null;
  source: string | null;
}

/** The panel's whole input, normalised from the endpoint's payload. */
export interface FireActivity {
  available: boolean;
  /** Why there is nothing to draw. A refusal is an answer, not an error. */
  unavailable_reason: string | null;
  bbox: FireBBox | null;
  cityBBox: FireBBox | null;
  cityLabel: string;
  regionLabel: string;
  detections: FireDetection[];
  regionalCount: number | null;
  inCityCount: number | null;
  source: string | null;
  weather: FireWeather | null;
}

// ---------------------------------------------------------------- normalising

/**
 * The endpoint is being written in parallel with this panel, so the reader is
 * deliberately tolerant about key names and strict about meaning: a field it
 * cannot find becomes `null` and renders as "not reported", never as a zero.
 */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function num(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function pick(source: Record<string, unknown>, keys: readonly string[]): unknown {
  for (const key of keys) {
    if (source[key] !== undefined && source[key] !== null) return source[key];
  }
  return undefined;
}

function readBBox(value: unknown): FireBBox | null {
  if (Array.isArray(value) && value.length >= 4) {
    // FIRMS and friends order a bbox west, south, east, north.
    const west = num(value[0]);
    const south = num(value[1]);
    const east = num(value[2]);
    const north = num(value[3]);
    if (west === null || south === null || east === null || north === null) return null;
    return { west, south, east, north };
  }
  if (!isRecord(value)) return null;
  const west = num(pick(value, ['west', 'min_lon', 'min_longitude', 'lon_min', 'left']));
  const south = num(pick(value, ['south', 'min_lat', 'min_latitude', 'lat_min', 'bottom']));
  const east = num(pick(value, ['east', 'max_lon', 'max_longitude', 'lon_max', 'right']));
  const north = num(pick(value, ['north', 'max_lat', 'max_latitude', 'lat_max', 'top']));
  if (west === null || south === null || east === null || north === null) return null;
  return { west, south, east, north };
}

function readDetections(value: unknown): FireDetection[] {
  if (!Array.isArray(value)) return [];
  const out: FireDetection[] = [];
  for (const row of value) {
    if (!isRecord(row)) continue;
    const latitude = num(pick(row, ['latitude', 'lat']));
    const longitude = num(pick(row, ['longitude', 'lon', 'lng', 'long']));
    // A detection without a position cannot be drawn and is not invented onto
    // the map at the centre of the box.
    if (latitude === null || longitude === null) continue;
    const rawConfidence = pick(row, ['confidence', 'confidence_label']);
    out.push({
      latitude,
      longitude,
      confidence:
        typeof rawConfidence === 'string' || typeof rawConfidence === 'number'
          ? rawConfidence
          : null,
      frp: num(pick(row, ['frp', 'fire_radiative_power', 'frp_mw'])),
      acquired_at: str(pick(row, ['acquired_at', 'acquired', 'observed_at'])),
      satellite: str(pick(row, ['satellite', 'instrument', 'source'])),
    });
  }
  return out;
}

function readWeather(value: unknown): FireWeather | null {
  if (!isRecord(value)) return null;
  const window = pick(value, ['window', 'observation_window', 'observed_window']);
  const windowRecord = isRecord(window) ? window : null;
  return {
    available: typeof value.available === 'boolean' ? value.available : true,
    unavailable_reason: str(pick(value, ['unavailable_reason', 'reason', 'refusal'])),
    temperature_c: num(pick(value, ['temperature_c', 'temperature', 'temp_c', 'air_temperature_c'])),
    relative_humidity_pct: num(
      pick(value, ['relative_humidity_pct', 'relative_humidity', 'humidity_pct', 'rh_pct']),
    ),
    wind_speed_ms: num(pick(value, ['wind_speed_ms', 'wind_ms', 'wind_speed', 'wind_speed_m_s'])),
    wind_direction_deg: num(pick(value, ['wind_direction_deg', 'wind_dir_deg', 'wind_direction'])),
    observation_start: str(
      pick(value, ['observation_start', 'observed_from', 'window_start', 'start']) ??
        (windowRecord ? pick(windowRecord, ['start', 'from', 'begin']) : undefined),
    ),
    observation_end: str(
      pick(value, ['observation_end', 'observed_to', 'window_end', 'end']) ??
        (windowRecord ? pick(windowRecord, ['end', 'to', 'until']) : undefined),
    ),
    observation_window: typeof window === 'string' ? window : null,
    source: str(pick(value, ['source', 'provider', 'dataset'])),
  };
}

function inside(box: FireBBox, detection: FireDetection): boolean {
  return (
    detection.longitude >= box.west &&
    detection.longitude <= box.east &&
    detection.latitude >= box.south &&
    detection.latitude <= box.north
  );
}

/**
 * Read the endpoint's payload into the shape this panel draws.
 *
 * Returns `null` only for a payload that is not an object at all -- everything
 * else becomes a `FireActivity` that either has detections or says why it does
 * not.
 */
export function normalizeFireActivity(raw: unknown): FireActivity | null {
  if (!isRecord(raw)) return null;

  const detections = readDetections(pick(raw, ['detections', 'fires', 'hotspots']));
  // `region` and `city` are what this backend actually sends. The rest are
  // kept because a FIRMS-shaped payload names the same thing several ways, and
  // reading only the alternatives is how this map spent its life saying "no
  // bounding box reported" while holding one.
  const bbox = readBBox(
    pick(raw, ['bbox', 'bbox_queried', 'query_bbox', 'region_bbox', 'queried_bbox', 'region']),
  );
  const cityBBox = readBBox(
    pick(raw, ['city_bbox', 'in_city_bbox', 'municipality_bbox', 'city']),
  );

  const counts = pick(raw, ['counts', 'count']);
  const countsRecord = isRecord(counts) ? counts : null;

  const regionalCount =
    num(pick(raw, ['regional_count', 'region_count', 'regional_detections'])) ??
    (countsRecord ? num(pick(countsRecord, ['regional', 'region', 'total'])) : null) ??
    (detections.length > 0 ? detections.length : null);

  const inCityCount =
    num(pick(raw, ['in_city_count', 'city_count', 'in_city_detections'])) ??
    (countsRecord ? num(pick(countsRecord, ['in_city', 'city'])) : null) ??
    // Only computable, never guessed: with a city box the arithmetic is on
    // data the payload supplied. Without one there is no honest answer.
    (cityBBox ? detections.filter((d) => inside(cityBBox, d)).length : null);

  const availableFlag = pick(raw, ['available', 'ok']);
  const available =
    typeof availableFlag === 'boolean'
      ? availableFlag
      : detections.length > 0 || bbox !== null || regionalCount !== null;

  return {
    available,
    unavailable_reason: str(pick(raw, ['unavailable_reason', 'reason', 'refusal', 'detail'])),
    bbox,
    cityBBox,
    cityLabel: str(pick(raw, ['city_label', 'city', 'municipality'])) ?? 'San Francisco',
    regionLabel: str(pick(raw, ['region_label', 'region_name'])) ?? 'the region',
    detections,
    regionalCount,
    inCityCount,
    source: str(pick(raw, ['source', 'product', 'dataset'])),
    weather: readWeather(pick(raw, ['fire_weather', 'weather'])),
  };
}

// --------------------------------------------------------------------- render

type Band = 'high' | 'nominal' | 'low' | 'unreported';



/** VIIRS letters, MODIS percentages, and the absence of either. */
export function confidenceBand(confidence: string | number | null): Band {
  if (confidence === null || confidence === undefined) return 'unreported';
  if (typeof confidence === 'number') {
    if (!Number.isFinite(confidence)) return 'unreported';
    return confidence >= 80 ? 'high' : confidence >= 30 ? 'nominal' : 'low';
  }
  const token = confidence.trim().toLowerCase();
  if (token === '') return 'unreported';
  if (token === 'h' || token === 'high') return 'high';
  if (token === 'n' || token === 'nominal') return 'nominal';
  if (token === 'l' || token === 'low') return 'low';
  const parsed = Number(token);
  return Number.isFinite(parsed) ? confidenceBand(parsed) : 'unreported';
}


/** Radius in user units, from fire radiative power. Area, not radius, scales. */

/** A day, in UTC, so a label does not move with the reader's timezone. */
function day(iso: string | null): string | null {
  if (!iso) return null;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toISOString().slice(0, 10);
}

/** The window the reanalysis covers, exactly as the payload reports it. */
export function windowLabel(weather: FireWeather | null): string | null {
  if (!weather) return null;
  if (weather.observation_window) return weather.observation_window;
  const start = day(weather.observation_start);
  const end = day(weather.observation_end);
  if (start && end) return start === end ? start : `${start} → ${end}`;
  return start ?? end;
}

function Reading({
  label,
  value,
  unit,
  window,
}: {
  label: string;
  value: string | null;
  unit: string;
  window: string | null;
}) {
  return (
    <div className="min-w-0 border border-line bg-surface px-2 py-1">
      <dt className="text-micro uppercase tracking-widest text-muted">{label}</dt>
      <dd>
        {value === null ? (
          <span className="font-mono text-micro text-muted">not reported</span>
        ) : (
          <span className="font-mono text-base leading-none text-ink">
            {value}
            <span className="ml-0.5 text-micro text-muted">{unit}</span>
          </span>
        )}
        {/* Each reading carries the window it was measured over: a number with
            no window is the reading pretending to be current. */}
        <span className="mt-0.5 block truncate text-micro text-muted">
          {window ?? 'window not reported'}
        </span>
      </dd>
    </div>
  );
}

export interface FireActivityMapProps {
  activity: FireActivity | null;
  /** A failed request, as distinct from an answered one carrying a refusal. */
  error?: string | null;
}

export function FireActivityMap({ activity, error = null }: FireActivityMapProps) {
  const heading = (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <h2 id="fire-activity-heading" className="text-micro uppercase tracking-widest text-muted">
        Regional fire activity
      </h2>
      {activity?.source && (
        <span className="font-mono text-micro text-muted">{activity.source}</span>
      )}
    </div>
  );

  if (error) {
    return (
      <section
        aria-labelledby="fire-activity-heading"
        className="shrink-0 bg-ground px-4 py-3"
        data-testid="fire-activity"
      >
        {heading}
        <p className="mt-2 border border-dashed border-alarm px-3 py-2 text-micro text-alarm">
          Fire-activity request failed: {error}
        </p>
      </section>
    );
  }

  if (!activity || !activity.available) {
    return (
      <section
        aria-labelledby="fire-activity-heading"
        className="shrink-0 bg-ground px-4 py-3"
        data-testid="fire-activity"
      >
        {heading}
        <p className="mt-2 border border-dashed border-line px-3 py-2 text-micro text-muted">
          Fire activity UNAVAILABLE —{' '}
          {activity?.unavailable_reason ?? 'the backend reported none. Nothing is inferred here.'}
        </p>
      </section>
    );
  }

  const weather = activity.weather;
  const window = windowLabel(weather);
  // Every reading absent is one fact, not three. A three-column grid saying
  // "not reported" three times, each under its own window, spent a third of
  // this panel restating a single absence -- and the absence is the ordinary
  // case, because POWER reanalysis lags by days.
  const noReadings =
    !!weather &&
    weather.available &&
    weather.temperature_c === null &&
    weather.relative_humidity_pct === null &&
    weather.wind_speed_ms === null;
  const cityCount = activity.inCityCount;
  const regionCount = activity.regionalCount;

  return (
    <section
      aria-labelledby="fire-activity-heading"
      className="shrink-0 bg-ground px-4 py-3"
      data-testid="fire-activity"
    >
      {heading}

      {/* The count line, and the whole point of the panel. An empty city
          inside a busy region is the normal reading, stated as a fact: not
          coloured as an alarm, not shaped as a failure. */}
      <p className="mt-2 text-micro text-ink" data-testid="fire-activity-counts">
        {cityCount === null ? (
          <span className="text-muted">In-city count not reported</span>
        ) : (
          <>
            <span className="font-mono text-base text-ink">{cityCount}</span>{' '}
            {cityCount === 1 ? 'active detection' : 'active detections'} in {activity.cityLabel}
          </>
        )}
        <span className="text-muted"> · </span>
        {regionCount === null ? (
          <span className="text-muted">regional count not reported</span>
        ) : (
          <>
            <span className="font-mono text-base text-ink">{regionCount}</span>{' '}
            <span className="text-muted">across {activity.regionLabel}</span>
          </>
        )}
      </p>
      {/* A standing explanation: true on every render, read once. It stays on
          the page and stops occupying two lines above the finding. */}
      <details className="mt-0.5">
        <summary className="cursor-pointer text-micro text-muted hover:text-ink">
          Why the city is always empty
        </summary>
        <p className="mt-1 text-micro leading-5 text-muted">
          VIIRS pixels are ~375 m and built for wildfire, so a structure fire never registers here.
          An empty city inside a busy region is the instrument working, not a fault.
        </p>
      </details>

      <div className="mt-3" data-testid="fire-weather">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h3 className="text-micro uppercase tracking-widest text-muted">Fire weather</h3>
          <span className="text-micro text-muted">{weather?.source ?? 'NASA POWER'}</span>
        </div>

        {noReadings ? (
          // Still says which window it looked at and still says it is
          // reanalysis: the absence is reported, not hidden.
          <p className="mt-1 text-micro leading-5 text-muted" data-testid="fire-weather-absent">
            No temperature, humidity or wind reported for {window ?? 'the requested window'}.{' '}
            <span className="text-disputed">
              Reanalysis, days behind real time — not the live NWS wind shown elsewhere.
            </span>
          </p>
        ) : weather && weather.available ? (
          <>
            <dl className="mt-1 grid grid-cols-3 gap-1">
              <Reading
                label="Temp"
                value={weather.temperature_c === null ? null : weather.temperature_c.toFixed(1)}
                unit="°C"
                window={window}
              />
              <Reading
                label="RH"
                value={
                  weather.relative_humidity_pct === null
                    ? null
                    : Math.round(weather.relative_humidity_pct).toString()
                }
                unit="%"
                window={window}
              />
              <Reading
                label="Wind"
                value={weather.wind_speed_ms === null ? null : weather.wind_speed_ms.toFixed(1)}
                unit={
                  weather.wind_direction_deg === null
                    ? 'm/s'
                    : `m/s @ ${Math.round(weather.wind_direction_deg)}°`
                }
                window={window}
              />
            </dl>
            <p className="mt-1 text-micro leading-5 text-disputed">
              Reanalysis, days behind real time: recent conditions, not current. This is not the
              live NWS wind shown elsewhere on the console.
            </p>
          </>
        ) : (
          <p className="mt-1 border border-dashed border-line px-3 py-2 text-micro text-muted">
            Fire weather UNAVAILABLE —{' '}
            {weather?.unavailable_reason ?? 'the backend reported none. Nothing is inferred here.'}
          </p>
        )}
      </div>
    </section>
  );
}
