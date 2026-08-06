"""Deterministic, source-grounded document processing primitives."""

from docspec.processing.artifacts import (
    RepresentationPayload,
    SegmentPayload,
    verify_representation_evidence,
    verify_segment_evidence,
    verify_segment_representation,
)
from docspec.domain.content import EvidenceMapping
from docspec.processing.extraction import (
    DefaultExtractorRegistry,
    ExtractionReceipt,
    ExtractionResult,
    HtmlExtractor,
    ImageExtractor,
    JsonExtractor,
    LazyPypdfExtractor,
    TextExtractor,
    XmlExtractor,
)
from docspec.processing.processors import (
    ContentStatisticsProcessor,
    ProcessorResult,
)
from docspec.domain.processors import (
    ProcessorCacheMode,
    ProcessorCachePolicy,
    ProcessorDescription,
    ProcessorInput,
    ProcessorItemLimits,
    ProcessorResourceIdentity,
    ProcessorResourceKind,
)
from docspec.processing.segmentation import (
    DefaultSegmenterRegistry,
    PageSegmenter,
    ParagraphSegmenter,
    RecordSegmenter,
    SegmentationReceipt,
    WholeImageSegmenter,
)

__all__ = [
    "ContentStatisticsProcessor",
    "DefaultExtractorRegistry",
    "DefaultSegmenterRegistry",
    "EvidenceMapping",
    "ExtractionReceipt",
    "ExtractionResult",
    "HtmlExtractor",
    "ImageExtractor",
    "JsonExtractor",
    "LazyPypdfExtractor",
    "PageSegmenter",
    "ParagraphSegmenter",
    "ProcessorCacheMode",
    "ProcessorCachePolicy",
    "ProcessorDescription",
    "ProcessorInput",
    "ProcessorItemLimits",
    "ProcessorResourceIdentity",
    "ProcessorResourceKind",
    "ProcessorResult",
    "RecordSegmenter",
    "RepresentationPayload",
    "SegmentationReceipt",
    "SegmentPayload",
    "TextExtractor",
    "WholeImageSegmenter",
    "XmlExtractor",
    "verify_representation_evidence",
    "verify_segment_evidence",
    "verify_segment_representation",
]
