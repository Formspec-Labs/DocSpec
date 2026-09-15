# Complete image test files

These are generated test images, not publisher captures. Each encodes a 31×17
RGB solid color `(12, 45, 78)` using Pillow's PNG, GIF or JPEG writer. All three
were independently opened and fully decoded with Pillow when generated on
2026-09-15. The receiver tests use these retained bytes without requiring Pillow.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `generated-31x17.png` | 98 | `db00445fada92ec7fe72f084bb1cfe93cc685eaca32757fe58785f717e6f1854` |
| `generated-31x17.gif` | 78 | `61974fde323e3e3ef4f5b38fd66d5d830c07c7bd68356aa67c8da7893a0f3f79` |
| `generated-31x17.jpeg` | 645 | `349fe743d789c94e5da9edaef7d9f0975e656d77e7e1f08b7e31ec532400acfd` |

The tests also contain explicit incomplete/malformed headers. Those distinguish
raw declared dimensions from a successfully decoded image and freeze each
deliberate change from the old DocSpec header reader.
