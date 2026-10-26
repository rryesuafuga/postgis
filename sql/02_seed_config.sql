-- Alert levels and trigger rules.
-- DEMONSTRATION THRESHOLDS. Operational thresholds, and the mapping of levels to CAP
-- urgency/severity/certainty, are for UNMA and the national Early Warning Technical Working
-- Group to agree; warning authority rests with the designated national approving authority.

INSERT INTO ews.alert_level (level, rank, cap_severity, cap_urgency, cap_certainty, cap_response, colour) VALUES
    -- colours follow the reserved status palette (always shown with a label, never colour alone)
    ('none',    0, 'Unknown',  'Unknown',  'Unknown',  'None',    '#f0efec'),
    ('watch',   1, 'Moderate', 'Future',   'Possible', 'Monitor', '#fab219'),
    ('warning', 2, 'Severe',   'Expected', 'Likely',   'Prepare', '#d03b3b')
ON CONFLICT (level) DO UPDATE SET
    rank = EXCLUDED.rank, cap_severity = EXCLUDED.cap_severity, cap_urgency = EXCLUDED.cap_urgency,
    cap_certainty = EXCLUDED.cap_certainty, cap_response = EXCLUDED.cap_response, colour = EXCLUDED.colour;

INSERT INTO ews.trigger_rule (rule_code, description, accum_days, metric, threshold, min_accum_mm, level, active, note) VALUES
    ('P95_MEAN',
     '3-day district-mean rainfall above the district-mean 95th percentile of 1991-2020 3-day totals for this time of year (and at least 40 mm)',
     3, 'mean_over_p95', 1.0, 40, 'watch', true, 'Climatological trigger'),
    ('P95_AREA50',
     'At least half of the district above its local 1991-2020 95th percentile of 3-day totals (and a district mean of at least 50 mm)',
     3, 'area_frac_over_p95', 0.5, 50, 'warning', true, 'Climatological trigger, spatial extent'),
    ('FIXED_MAX_100',
     'Wettest location in the district received at least 100 mm in 3 days',
     3, 'max_mm', 100, 0, 'watch', true, 'Fixed demonstration trigger'),
    ('FIXED_MEAN_80',
     'District-mean 3-day rainfall of at least 80 mm',
     3, 'mean_mm', 80, 0, 'warning', true, 'Fixed demonstration trigger')
ON CONFLICT (rule_code) DO UPDATE SET
    description = EXCLUDED.description, accum_days = EXCLUDED.accum_days, metric = EXCLUDED.metric,
    threshold = EXCLUDED.threshold, min_accum_mm = EXCLUDED.min_accum_mm, level = EXCLUDED.level,
    note = EXCLUDED.note;
