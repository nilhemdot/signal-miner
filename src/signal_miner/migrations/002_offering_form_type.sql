-- Same-day ordering of filings: an amendment beats the original it amends, so the offering needs the latest form type.
SET ROLE pipeline_writer;
ALTER TABLE sm.form_d_offerings ADD COLUMN latest_form_type text NOT NULL DEFAULT 'D' CHECK (latest_form_type IN ('D', 'D/A'));
UPDATE sm.form_d_offerings o SET latest_form_type = f.form_type FROM sm.form_d_filings f WHERE f.accession = o.latest_accession;
RESET ROLE;
