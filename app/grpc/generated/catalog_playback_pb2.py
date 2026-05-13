# -*- coding: utf-8 -*-
# Generated from contracts/catalog/catalog_playback.proto. Do not edit manually.
# source: catalog/catalog_playback.proto
"""Generated protocol buffer code for CatalogPlaybackService."""

from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import builder as _builder

_sym_db = _symbol_database.Default()

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n\x1ecatalog/catalog_playback.proto\x12\x16streambuted.catalog.v1'
    b'"+\n\x17GetPlayableTrackRequest\x12\x10\n\x08track_id\x18\x01 \x01(\t'
    b'"{\n\x15PlayableTrackResponse\x12\x10\n\x08track_id\x18\x01 \x01(\t'
    b'\x12\x0e\n\x06status\x18\x02 \x01(\t\x12\x16\n\x0eaudio_asset_id'
    b'\x18\x03 \x01(\t\x12\x18\n\x10duration_seconds\x18\x04 \x01(\x01'
    b'\x12\x0e\n\x06exists\x18\x05 \x01(\x082\x8c\x01\n'
    b'\x16CatalogPlaybackService\x12r\n\x10GetPlayableTrack\x12/.'
    b'streambuted.catalog.v1.GetPlayableTrackRequest\x1a-.'
    b'streambuted.catalog.v1.PlayableTrackResponseb\x06proto3'
)

_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, globals())
_builder.BuildTopDescriptorsAndMessages(
    DESCRIPTOR,
    "app.grpc.generated.catalog_playback_pb2",
    globals(),
)
if _descriptor._USE_C_DESCRIPTORS is False:
    DESCRIPTOR._options = None
