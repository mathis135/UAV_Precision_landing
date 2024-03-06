from square_detect import detect_square_main, check_for_time
from coordinate_transform import transform_to_ground_xy, calculate_new_coordinate
from hogh_circles import concentric_circles

import cv2 as cv
import numpy as np
from enum import Enum

from pynput import keyboard

def on_press(key):
    if key == keyboard.Key.tab:
        global skip###Parameters
# cannyEdgeMaxThr = 40 #Max Thr for canny edge detection
# circleDetectThr = 35 #Threshold for circle detection
# size = 30           #Size of the circles (to be calculated)
# factor = 3.2          #Factor big circle diameter / small circle diameter
# rangePerc = 1.5     #This is the range the circles are expected to be in
        skip = True
    #try:
        #print('alphanumeric key {0} pressed'.format(key.char))
    #except AttributeError:
        #print('special key {0} pressed'.format(key))

def on_release(key):
    if key == keyboard.Key.esc:
        # Stop listener
        return False

# ...or, in a non-blocking fashion:
listener = keyboard.Listener(
    on_press=on_press,
    on_release=on_release)
listener.start()

class state(Enum):
    initial = 0
    fly_to_waypoint = 1
    detect_square = 2
    fly_to_target = 3
    check_for_target = 4
    descend_square = 5
    descend_concentric = 6
    descend_inner_circle = 7
    land_tins = 8
    return_to_launch = 9

class error_estimation_image:
    def __init__(self):
        self.x = 0
        self.y = 0
        self.z = 0
        self.timeout = 0

class uav:
    def __init__(self):
        self.angle_x = 0
        self.angle_y = 0
        self.altitude = 10
        self.heading = 0
        self.latitude = 29.183972
        self.longitude = -81.043251
        self.state = state.initial
        self.cam_hfov = 65
        self.cam_vfov = 52

def main():
    global skip
    skip = False
    video = cv.VideoCapture("images/cut.mp4")

    #only to not play the entire video
    fps = video.get(cv.CAP_PROP_FPS)
    start_time_video = 4
    start_frame_num = int(start_time_video * fps)
    video.set(cv.CAP_PROP_POS_FRAMES, start_frame_num)

    err_estimation = error_estimation_image()  # Create an instance of the error_estimation class
    uav_inst = uav()  # Create an instance of the uav_angles class
    uav_inst.state = state.descend_square
    check_for_time.start_time = None
    reached_waypoint = "n"
    reached_target = "n"
    while video.isOpened():
        ret, frame = video.read()
        if ret:
            
            if uav_inst.state == state.descend_concentric:
                frame = cv.resize(frame, (850, 478))
            else:
                frame = cv.resize(frame, (640, 480))
            match uav_inst.state:
                case state.initial:
                    uav_inst.state = state.fly_to_waypoint

                case state.fly_to_waypoint:
                    #############################################
                    ###Fly to waypoint using lat and lon#########
                    #############################################

                    ##This check will be done with mavros data
                    if reached_waypoint == "n":
                        reached_waypoint = input("Have you reached the waypoint? (y/n)")
                    if reached_waypoint == "y":
                        uav_inst.state = state.detect_square

                case state.detect_square:                    
                    #Check for 3s if square is detected more than 50% of the time
                    square_detected_err = check_for_time(frame=frame, altitude=uav_inst.altitude,duration=3,ratio_detected=0.5)
                    if square_detected_err is None:
                        pass #time not over yet
                    elif square_detected_err is False:
                        uav_inst.state = state.fly_to_waypoint
                        reached_waypoint = "n"
                        print("Square not detected. Flying to next waypoint.")
                        ################################################
                        ####Fly to next waypoint, no square detected####
                        ################################################
                        break
                    else:
                        uav_inst.state = state.fly_to_target
                        print("Square detected: " + str(square_detected_err))

                        #Transform the error to target to lat and lon
                        err_estimation.x = square_detected_err[0]
                        err_estimation.y = square_detected_err[1]
                        error_ground_xy = transform_to_ground_xy([err_estimation.x, err_estimation.y], [uav_inst.angle_x, uav_inst.angle_y], uav_inst.altitude)
                        target_lat, target_lon = calculate_new_coordinate(uav_inst.latitude, uav_inst.longitude, error_ground_xy, uav_inst.heading)
                    
                case state.fly_to_target:
                    #############################################
                    #Fly to estimated waypoint using lat and lon#
                    #############################################


                    #Detect square again
                    #Check for 2s if square is detected
                    #check will be done using mavros data
                    if reached_target == "n":
                        reached_target = input("Have you reached the target? (y/n)")
                    if reached_target == "y":
                        check_for_time.start_time = None
                        print("restart time")
                        uav_inst.state = state.check_for_target
                        
                case state.check_for_target:
                    #check for 2s if square is detected more than 80% of the time
                    square_detected_err = check_for_time(frame=frame, altitude=uav_inst.altitude,duration=2,ratio_detected=0.9)

                    if square_detected_err is None:
                        continue #time not over yet
                    elif square_detected_err is False:
                        uav_inst.state = state.return_to_launch
                        reached_target = "over"
                        print("Returning to launch")
                    else:
                        #Set mode to using square as reference
                        uav_inst.state = state.descend_square
                        reached_target = "over"
                        print("Target detected: " + str(square_detected_err) + "now descending")

                case state.descend_square:
                    error_xy, area_ratio = detect_square_main(frame, 10)
                    if error_xy is not None:
                        err_estimation.x = error_xy[0]
                        err_estimation.y = error_xy[1]
                        error_ground_xy = transform_to_ground_xy([err_estimation.x, err_estimation.y], [uav_inst.angle_x, uav_inst.angle_y], uav_inst.altitude)
                        print("Area ratio " + str(area_ratio))
                    if skip: #should instead be a check whether area ratio is bigger than threshold
                        uav_inst.state = state.descend_concentric
                        skip = False
                        video = cv.VideoCapture("images/concentric_to_single_rot.mp4")
                    

                case state.descend_concentric:
                    concentric_circles(frame=frame, altitude=.5, cam_hfov=uav_inst.cam_hfov) 
                    print("Descend concentric")
                    pass
                case state.descend_inner_circle:
                    pass
                case state.land_tins:
                    pass
                case state.return_to_launch:
                    pass
        else:
            break

      

if __name__ == "__main__":
    main()
