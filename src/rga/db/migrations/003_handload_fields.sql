-- 003_handload_fields: a handload is described by its bullet (weight and line), primer, brass and COAL.
-- Powder and charge move into the load's notes; a bullet text such as "116 TMK" or "140gr ELD-M" is
-- split into weight and line. The retired columns (powder, charge_gr) stay in place, unused.
PRAGMA foreign_keys = ON;

ALTER TABLE loads ADD COLUMN brass TEXT;
ALTER TABLE loads ADD COLUMN coal_in REAL CHECK (coal_in IS NULL OR (coal_in >= 1 AND coal_in <= 5));

UPDATE loads
SET notes = notes
  || CASE WHEN notes <> '' THEN char(10) ELSE '' END
  || CASE
       WHEN nullif(trim(powder), '') IS NOT NULL AND charge_gr IS NOT NULL
         THEN 'Powder: ' || trim(powder) || ', charge: ' || printf('%g', charge_gr) || ' gr'
       WHEN nullif(trim(powder), '') IS NOT NULL
         THEN 'Powder: ' || trim(powder)
       ELSE 'Charge: ' || printf('%g', charge_gr) || ' gr'
     END
WHERE kind = 'handload' AND (nullif(trim(powder), '') IS NOT NULL OR charge_gr IS NOT NULL);

-- A leading weight of 10 to 1000 (optionally followed by gr, grain or grains) is split off the bullet
-- text. Text that does not start with such a weight, or whose remainder holds another number
-- ("300 Win Mag 180 gr"), is kept whole as the line.
WITH RECURSIVE scan(id, txt, pos) AS (
  SELECT id, ltrim(bullet), 1 FROM loads
  WHERE kind = 'handload' AND bullet_weight_gr IS NULL AND nullif(trim(bullet), '') IS NOT NULL
  UNION ALL
  SELECT id, txt, pos + 1 FROM scan
  WHERE pos <= length(txt) AND substr(txt, pos, 1) GLOB '[0-9.]'
),
prefix AS (
  SELECT id, txt, max(pos) - 1 AS n FROM scan GROUP BY id
),
rest AS (
  SELECT id, txt, substr(txt, 1, n) AS num, lower(ltrim(substr(txt, n + 1), ' -')) AS low,
         ltrim(substr(txt, n + 1), ' -') AS raw
  FROM prefix WHERE n > 0
),
parsed AS (
  SELECT id, num,
         trim(
           CASE
             WHEN low = 'grains' OR low GLOB 'grains[^a-z]*' THEN substr(raw, 7)
             WHEN low = 'grain' OR low GLOB 'grain[^a-z]*' THEN substr(raw, 6)
             WHEN low = 'gr' OR low GLOB 'gr[^a-z]*' THEN ltrim(substr(raw, 3), '.')
             ELSE raw
           END, ' -,'
         ) AS line
  FROM rest
  WHERE num GLOB '[0-9]*' AND num NOT GLOB '*.*.*' AND num NOT GLOB '*.'
    AND CAST(num AS REAL) BETWEEN 10 AND 1000
)
UPDATE loads
SET bullet_weight_gr = CAST(parsed.num AS REAL), bullet = nullif(parsed.line, '')
FROM parsed
WHERE loads.id = parsed.id AND parsed.line NOT GLOB '*[0-9]*';

PRAGMA user_version = 3;
