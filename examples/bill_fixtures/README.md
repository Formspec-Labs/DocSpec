# Synthetic bill inputs

These three files are authored test data, not captured congressional documents.
They use a real bill identifier and canonical publisher URLs to exercise identity
checks. Their title, dates, and document text are deliberately invented.

`status.xml` lists the engrossed version first and the introduced version second.
Both offer XML; the status also preserves HTML/PDF links that the example does
not fetch. Selecting the introduced package must not silently fetch the first or
newest version. Its text contains “machine readable”; the engrossed text does not.

The fixtures exercise XML acquisition and lexical processing, not validation
against the complete legislative DTD, live availability, or legal interpretation.
They contain no redactions or credentials. The example retains the original bytes
and computed SHA-256 values in its output.
