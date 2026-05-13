# Generated from contracts/catalog/catalog_playback.proto. Do not edit manually.
"""Generated gRPC bindings for CatalogPlaybackService."""

import grpc

from app.grpc.generated import catalog_playback_pb2 as catalog__playback__pb2


class CatalogPlaybackServiceStub:
    """Client stub for CatalogPlaybackService."""

    def __init__(self, channel: grpc.Channel) -> None:
        """Create a CatalogPlaybackService client stub."""
        self.GetPlayableTrack = channel.unary_unary(
            "/streambuted.catalog.v1.CatalogPlaybackService/GetPlayableTrack",
            request_serializer=catalog__playback__pb2.GetPlayableTrackRequest.SerializeToString,
            response_deserializer=catalog__playback__pb2.PlayableTrackResponse.FromString,
        )


class CatalogPlaybackServiceServicer:
    """Server API for CatalogPlaybackService."""

    def GetPlayableTrack(self, request, context):
        """Return metadata required to decide track playback eligibility."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Method not implemented.")
        raise NotImplementedError("Method not implemented.")


def add_CatalogPlaybackServiceServicer_to_server(
    servicer: CatalogPlaybackServiceServicer,
    server: grpc.Server,
) -> None:
    """Register a CatalogPlaybackService implementation with a gRPC server."""
    rpc_method_handlers = {
        "GetPlayableTrack": grpc.unary_unary_rpc_method_handler(
            servicer.GetPlayableTrack,
            request_deserializer=catalog__playback__pb2.GetPlayableTrackRequest.FromString,
            response_serializer=catalog__playback__pb2.PlayableTrackResponse.SerializeToString,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "streambuted.catalog.v1.CatalogPlaybackService",
        rpc_method_handlers,
    )
    server.add_generic_rpc_handlers((generic_handler,))
