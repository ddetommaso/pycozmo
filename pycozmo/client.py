
from threading import Event
from typing import Optional, Tuple
import json
import time
import io

import numpy as np
from PIL import Image

from .logging import logger
from . import protocol_encoder
from . import event
from . import camera
from . import object
from . import util
from . import robot
from . import exception
from . import filter
from . import protocol_declaration
from . import conn


class EvtRobotFound(event.Event):
    """ Triggered when the robot has been first connected. """


class EvtRobotReady(event.Event):
    """ Triggered when the robot has been initialized and is ready for commands. """


class EvtNewRawCameraImage(event.Event):
    """ Triggered when a new raw image is received from the robot's camera. """


class EvtRobotMovingChange(event.Event):
    pass


class EvtRobotCarryingBlockChange(event.Event):
    pass


class EvtRobotPickingOrPlacingChange(event.Event):
    pass


class EvtRobotPickedUpChange(event.Event):
    pass


class EvtRobotBodyAccModeChange(event.Event):
    pass


class EvtRobotFallingChange(event.Event):
    pass


class EvtRobotAnimatingChange(event.Event):
    pass


class EvtRobotPathingChange(event.Event):
    pass


class EvtRobotLiftInPositionChange(event.Event):
    pass


class EvtRobotHeadInPositionChange(event.Event):
    pass


class EvtRobotAnimBufferFullChange(event.Event):
    pass


class EvtRobotAnimatingIdleChange(event.Event):
    pass


class EvtRobotOnChargerChange(event.Event):
    pass


class EvtRobotChargingChange(event.Event):
    pass


class EvtCliffDetectedChange(event.Event):
    pass


class EvtRobotWheelsMovingChange(event.Event):
    pass


class EvtChargerOOSChange(event.Event):
    pass


STATUS_EVTS = {
    robot.RobotStatusFlag.IS_MOVING: EvtRobotMovingChange,
    robot.RobotStatusFlag.IS_CARRYING_BLOCK: EvtRobotCarryingBlockChange,
    robot.RobotStatusFlag.IS_PICKING_OR_PLACING: EvtRobotPickingOrPlacingChange,
    robot.RobotStatusFlag.IS_PICKED_UP: EvtRobotPickedUpChange,
    robot.RobotStatusFlag.IS_BODY_ACC_MODE: EvtRobotBodyAccModeChange,
    robot.RobotStatusFlag.IS_FALLING: EvtRobotFallingChange,
    robot.RobotStatusFlag.IS_ANIMATING: EvtRobotAnimatingChange,
    robot.RobotStatusFlag.IS_PATHING: EvtRobotPathingChange,
    robot.RobotStatusFlag.LIFT_IN_POS: EvtRobotLiftInPositionChange,
    robot.RobotStatusFlag.HEAD_IN_POS: EvtRobotHeadInPositionChange,
    robot.RobotStatusFlag.IS_ANIM_BUFFER_FULL: EvtRobotAnimBufferFullChange,
    robot.RobotStatusFlag.IS_ANIMATING_IDLE: EvtRobotAnimatingChange,
    robot.RobotStatusFlag.IS_ON_CHARGER: EvtRobotOnChargerChange,
    robot.RobotStatusFlag.IS_CHARGING: EvtRobotChargingChange,
    robot.RobotStatusFlag.CLIFF_DETECTED: EvtCliffDetectedChange,
    robot.RobotStatusFlag.ARE_WHEELS_MOVING: EvtRobotWheelsMovingChange,
    robot.RobotStatusFlag.IS_CHARGER_OOS: EvtChargerOOSChange,
}


class EvtRobotStateUpdated(event.Event):
    """ Triggered when a new robot state is received. """


class Client(event.Dispatcher):

    def __init__(self, robot_addr: Optional[Tuple[str, int]] = None,
                 protocol_log_messages: Optional[list] = None) -> None:
        super().__init__()
        self.conn = conn.ClientConnection(robot_addr, protocol_log_messages)
        self.serial_number_head = None
        self.robot_fw_sig = None
        self.serial_number = None
        self.body_hw_version = None
        self.body_color = None
        # Robot state
        # Heading in X-Y plane.
        self.pose_angle = util.Angle(radians=0.0)
        self.pose_pitch = util.Angle(radians=0.0)
        self.head_angle = util.Angle(radians=robot.MIN_HEAD_ANGLE.radians)
        self.left_wheel_speed = util.Speed(mmps=0.0)
        self.right_wheel_speed = util.Speed(mmps=0.0)
        self.lift_position = robot.LiftPosition(height=robot.MIN_LIFT_HEIGHT)
        self.battery_voltage = 0.0
        self.accel = util.Vector3(0.0, 0.0, 0.0)
        self.gyro = util.Vector3(0.0, 0.0, 0.0)
        self.robot_status = 0
        # Animation state
        self.num_anim_bytes_played = 0
        self.num_audio_frames_played = 0
        self.enabled_anim_tracks = 0
        self.tag = 0
        self.client_drop_count = 0
        # Camera state
        self.last_image_timestamp = None
        # Object state
        self.available_objects = dict()
        self.connected_objects = dict()
        # Filters
        self.packet_type_filter = filter.Filter()
        self.packet_type_filter.deny_ids({protocol_declaration.PacketType.PING.value})
        self.packet_id_filter = filter.Filter()
        self._reset_partial_state()

    def start(self) -> None:
        logger.debug("Starting client...")
        self.conn.add_handler(protocol_encoder.HardwareInfo, self._on_hardware_info)
        self.conn.add_handler(protocol_encoder.FirmwareSignature, self._on_firmware_signature)
        self.conn.add_handler(protocol_encoder.BodyInfo, self._on_body_info)
        self.conn.add_handler(protocol_encoder.ImageChunk, self._on_image_chunk)
        self.conn.add_handler(protocol_encoder.RobotState, self._on_robot_state)
        self.conn.add_handler(protocol_encoder.AnimationState, self._on_animation_state)
        self.conn.add_handler(protocol_encoder.ObjectAvailable, self._on_object_available)
        self.conn.add_handler(protocol_encoder.ObjectConnectionState, self._on_object_connection_state)
        self.conn.start()

    def stop(self) -> None:
        logger.debug("Stopping client...")
        self.conn.stop()
        self.del_all_handlers()

    def connect(self) -> None:
        logger.debug("Connecting...")
        self.conn.connect()

    def disconnect(self) -> None:
        logger.debug("Disconnecting...")
        self.conn.disconnect()

    def _enable_robot(self):
        # Enable
        pkt = protocol_encoder.Enable()
        self.conn.send(pkt)
        self.conn.send(pkt)  # This repetition seems to trigger BodyInfo

    def _initialize_robot(self):
        # Enables RobotState and ObjectAvailable events - enables body ACC? Requires 0x25.
        pkt = protocol_encoder.EnableBodyACC()
        self.conn.send(pkt)
        # Enables AnimationState events. Requires 0x25.
        pkt = protocol_encoder.EnableAnimationState()
        self.conn.send(pkt)

        # Initialize display.
        for _ in range(7):
            pkt = protocol_encoder.NextFrame()
            self.conn.send(pkt)
            pkt = protocol_encoder.DisplayImage(b"\x3f\x3f")
            self.conn.send(pkt)

        # TODO: This should not be necessary.
        time.sleep(0.5)

        self.dispatch(EvtRobotReady, self)

    def _on_hardware_info(self, cli, pkt: protocol_encoder.HardwareInfo):
        del cli
        self.serial_number_head = pkt.serial_number_head

    def _on_firmware_signature(self, cli, pkt: protocol_encoder.FirmwareSignature):
        del cli
        self.robot_fw_sig = json.loads(pkt.signature)
        logger.info("Firmware version %s.", self.robot_fw_sig["version"])
        self._enable_robot()

    def _on_body_info(self, cli, pkt: protocol_encoder.BodyInfo):
        del cli
        self.serial_number = pkt.serial_number
        self.body_hw_version = pkt.body_hw_version
        self.body_color = pkt.body_color
        logger.info("Body S/N 0x%08x.", self.serial_number)
        supported = self.robot_fw_sig["version"] == protocol_declaration.FIRMWARE_VERSION
        if supported:
            self._initialize_robot()
        else:
            logger.error("Unsupported Cozmo firmware version %i. Only version %i is supported currently.",
                         self.robot_fw_sig["version"], protocol_declaration.FIRMWARE_VERSION)
        self.dispatch(EvtRobotFound, self)

    def wait_for_robot(self, timeout: float = 5.0) -> None:
        if not self.robot_fw_sig:
            e = Event()
            self.add_handler(EvtRobotFound, lambda cli: e.set(), one_shot=True)
            if not e.wait(timeout):
                raise exception.ConnectionTimeout("Failed to connect to Cozmo.")

        if self.robot_fw_sig["version"] != protocol_declaration.FIRMWARE_VERSION:
            raise exception.UnsupportedFirmwareVersion("Unsupported Cozmo firmware version.")

        if not self.serial_number:
            e = Event()
            self.add_handler(EvtRobotReady, lambda cli: e.set(), one_shot=True)
            if not e.wait(timeout):
                raise exception.ConnectionTimeout("Failed to initialize Cozmo.")

    def _reset_partial_state(self):
        self._partial_image_timestamp = None
        self._partial_data = None
        self._partial_image_id = None
        self._partial_invalid = False
        self._partial_size = 0
        self._partial_image_encoding = None
        self._partial_image_resolution = None
        self._last_chunk_id = -1

    def _on_image_chunk(self, cli, pkt: protocol_encoder.ImageChunk):
        del cli
        if self._partial_image_id is not None and pkt.chunk_id == 0:
            if not self._partial_invalid:
                logger.debug("Lost final chunk of image - discarding.")
            self._partial_image_id = None

        if self._partial_image_id is None:
            if pkt.chunk_id != 0:
                if not self._partial_invalid:
                    logger.debug("Received chunk of broken image.")
                self._partial_invalid = True
                return
            # Discard any previous in-progress image
            self._reset_partial_state()
            self._partial_image_timestamp = pkt.frame_timestamp
            self._partial_image_id = pkt.image_id
            self._partial_image_encoding = protocol_encoder.ImageEncoding(pkt.image_encoding)
            self._partial_image_resolution = protocol_encoder.ImageResolution(pkt.image_resolution)

            image_resolution = protocol_encoder.ImageResolution(pkt.image_resolution)
            width, height = camera.RESOLUTIONS[image_resolution]
            max_size = width * height * 3  # 3 bytes per pixel (RGB)
            self._partial_data = np.empty(max_size, dtype=np.uint8)

        if pkt.chunk_id != (self._last_chunk_id + 1) or pkt.image_id != self._partial_image_id:
            logger.debug("Image missing chunks - discarding (last_chunk_id=%d partial_image_id=%s).",
                         self._last_chunk_id, self._partial_image_id)
            self._reset_partial_state()
            self._partial_invalid = True
            return

        offset = self._partial_size
        self._partial_data[offset:offset + len(pkt.data)] = pkt.data
        self._partial_size += len(pkt.data)
        self._last_chunk_id = pkt.chunk_id

        if pkt.chunk_id == pkt.image_chunk_count - 1:
            self._process_completed_image()
            self._reset_partial_state()

    def _process_completed_image(self):
        data = self._partial_data[0:self._partial_size]

        # The first byte of the image is whether or not it is in color
        is_color_image = data[0] != 0

        if self._partial_image_encoding == protocol_encoder.ImageEncoding.JPEGMinimizedGray:
            width, height = camera.RESOLUTIONS[self._partial_image_resolution]

            if is_color_image:
                # Color images are half width
                width = width // 2
                data = camera.minicolor_to_jpeg(data, width, height)
            else:
                data = camera.minigray_to_jpeg(data, width, height)

        image = Image.open(io.BytesIO(data)).convert('RGB')

        # Color images need to be resized to the proper resolution
        if is_color_image:
            size = camera.RESOLUTIONS[self._partial_image_resolution]
            image = image.resize(size)

        self._latest_image = image
        self.last_image_timestamp = self._partial_image_timestamp
        self.dispatch(EvtNewRawCameraImage, self, image)

    def _on_robot_state(self, cli, pkt: protocol_encoder.RobotState):
        del cli
        self.pose_angle = util.Angle(radians=pkt.pose_angle_rad)   # heading in X-Y plane
        self.pose_pitch = util.Angle(radians=pkt.pose_pitch_rad)
        self.head_angle = util.Angle(radians=pkt.head_angle_rad)
        self.left_wheel_speed = util.Speed(mmps=pkt.lwheel_speed_mmps)
        self.right_wheel_speed = util.Speed(mmps=pkt.rwheel_speed_mmps)
        self.lift_position = robot.LiftPosition(height=util.Distance(mm=pkt.lift_height_mm))
        self.battery_voltage = pkt.battery_voltage
        self.accel = util.Vector3(pkt.accel_x, pkt.accel_y, pkt.accel_z)
        self.gyro = util.Vector3(pkt.gyro_x, pkt.gyro_y, pkt.gyro_z)
        old_status = self.robot_status
        self.robot_status = pkt.status
        self.dispatch(EvtRobotStateUpdated, self)
        # Dispatch status flag change events.
        for flag, evt in STATUS_EVTS.items():
            if (old_status & flag) != (pkt.status & flag):
                state = (pkt.status & flag) != 0
                logger.debug("%s: %i", robot.RobotStatusFlagNames[flag], state)
                self.dispatch(evt, self, state)

    def _on_animation_state(self, cli, pkt: protocol_encoder.AnimationState):
        del cli
        self.num_anim_bytes_played = pkt.num_anim_bytes_played
        self.num_audio_frames_played = pkt.num_audio_frames_played
        self.enabled_anim_tracks = pkt.enabled_anim_tracks
        self.tag = pkt.tag
        self.client_drop_count = pkt.client_drop_count

    def _on_object_available(self, cli, pkt: protocol_encoder.ObjectAvailable):
        del cli
        factory_id = pkt.factory_id
        object_type = protocol_encoder.ObjectType(pkt.object_type)
        obj = object.Object(factory_id=factory_id, object_type=object_type)
        if factory_id not in self.available_objects:
            self.available_objects[factory_id] = obj
            logger.debug("Object of type %s with S/N 0x%08x available.", str(obj.object_type), obj.factory_id)

    def _on_object_connection_state(self, cli, pkt: protocol_encoder.ObjectConnectionState):
        del cli
        if pkt.connected:
            # Connected
            self.connected_objects[pkt.object_id] = {
                "factory_id": pkt.factory_id,
                "object_type": pkt.object_type,
            }
        else:
            # Disconnected
            if pkt.object_id in self.connected_objects:
                del self.connected_objects[pkt.object_id]
