# Canonical JSON and exact source bytes

DocSpec uses the installed Rulespec canonical JSON encoder for identities,
structured source metadata, processor output records and catalog digest records.
One implementation determines which JSON values can be exchanged exactly and
which bytes identify them.

The domain identity gateway is the one declared core dependency on the public
Rulespec package. Its own dependencies, including an optional encoder
accelerator when installed, load with that package. Concrete storage, transport
and processing implementations keep their existing adapter or selected-stage
boundaries. Import tests enforce this specific dependency rather than a broad
exception for third-party software throughout the core.

The supported values are null, booleans, exact integers from
−9,007,199,254,740,991 through 9,007,199,254,740,991, Unicode scalar strings,
arrays and objects with distinct string keys. Object keys sort by unsigned
UTF-16 code units. String code points remain unchanged, including distinct
composed and decomposed Unicode spellings. Floats, larger integers, lone
surrogates and unsupported Python objects refuse. DocSpec never rounds or
stringifies a refused value. A producer may deliberately describe a value as
text when that is the actual field meaning.

DocSpec still converts its dataclasses, Enum values, mappings and sequences
before encoding. It reports contextual conversion errors and applies the
newline framing required by each file type. These are DocSpec responsibilities;
they do not define a second encoder.

Captured documents and upstream evidence remain exact bytes. For example, an
HTML document containing `9223372036854775808` can be captured, retained and
read back unchanged. Putting that number into a structured metadata field as a
JSON integer refuses. This distinction also applies to portable export: copied
raw dependencies retain their byte pins; newly encoded records use the shared
JSON domain.

This adoption deliberately narrows the former permissive identity path and
changes key ordering where Python code-point ordering differed from UTF-16.
Valid ASCII-key records keep their encoding. Artifacts whose identity depended
on the old ordering must be rebuilt from their inputs; values outside the
supported domain must be corrected by their producer. There is no alternate
legacy encoder or silent identity translation.

The tests exercise the shared wheel's 44-case corpus, domain conversion,
portable JSON/JSONL refusal, public supplied-record metadata refusal and an
installed capture/retain/inspection path preserving the raw number above.
The incremental catalog framer remains because it lets several digests share
one bounded record scan. It emits each record through the same shared encoder.
