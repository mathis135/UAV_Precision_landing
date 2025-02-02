#!/usr/bin/env python3

from square_detect import detect_square_main, check_for_time
from coordinate_transform import transform_to_ground_xy, calculate_new_coordinate, transform_ground_to_img_xy
from hogh_circles import concentric_circles, small_circle
from debugging_code import display_error_and_text
import cv2 as cv
import numpy as np
from enum import Enum
from math import atan2, pi, sqrt, exp, cos, sin
import time


import rospy
from cv_bridge import CvBridge
from sensor_msgs.msg import *
from mavros_msgs.msg import *
from mavros_msgs.srv import *
from geometry_msgs.msg import *
from std_msgs.msg import *
from tf.transformations import quaternion_from_euler,euler_from_quaternion



class state:
    def __init__(self):
        self.initial = 0
        self.fly_to_waypoint = 1
        self.detect_square = 2
        self.fly_to_target = 3
        self.check_for_target = 4
        self.descend_square = 5
        self.descend_concentric = 6
        self.descend_inner_circle = 7
        self.detect_tins = 8
        self.fly_over_tins = 9
        self.land = 10
        self.return_to_launch = 11
        self.climb_to_altitude = 12
        self.done = 13
        self.align_before_landing = 14
    

class error_estimation:
    def __init__(self):
        #Errors in the image plane range from -1 to 1 in x and -0.75 to 0.75 in y
        self.x_img = 0
        self.y_img = 0
        #Current err in the ground plane depending on x_img and y_img and altitude of UAV
        self.x_m = 0
        self.y_m = 0

        #Filtered errors and altutude in the ground plane
        self.x_m_filt = []
        self.y_m_filt = []
        self.err_distances_filt = []
        self.err_times_filt = []
        self.altitude_m_filt = []

        #Average errors and altitude in the ground plane (after weighted average filter is applied)
        self.x_m_avg = 0
        self.y_m_avg = 0
        self.altitude_m_avg = 0

        self.altitude_m = 0
        self.ascended_by_m = 0 #How much the UAV has ascended by without reference target
        # self.target_lat = 0
        # self.target_lon = 0
        self.list_length = 5 #Number of elements to keep in the list (filter)
        #Errors in pixels for debugging
        self.err_px_x = 0 
        self.err_px_y = 0
        self.p_value = 0.95
        self.time_last_detection = None

    def update_errors(self, x_img, y_img, altitude_m, cam_fov_hv, image_size, heading):
        """Update the errors in the image and ground plane and the target lat and lon"""     
        #Assign current errors
        #take rotation of UAV into account (transformed)
        x_transformed = cos(heading)*x_img - sin(heading)*y_img
        y_transformed = sin(heading)*x_img + cos(heading)*y_img
        self.x_img = x_transformed
        self.y_img = y_transformed
        self.altitude_m = altitude_m
        #Calculate the errors in the ground plane (dependent on altitude of UAV)
        error_ground_xy = transform_to_ground_xy([x_img, y_img],altitude_m, cam_fov_hv)
        self.x_m = error_ground_xy[0]
        self.y_m = error_ground_xy[1]

        #Keep list a maximum of self.list_length elements
        if len(self.x_m_filt) >= self.list_length:
            self.x_m_filt.pop(0)
            self.y_m_filt.pop(0)
            self.err_distances_filt.pop(0)
            self.err_times_filt.pop(0)
            self.altitude_m_filt.pop(0)

        #Add the current errors to the list
        self.x_m_filt.append(self.x_m)
        self.y_m_filt.append(self.y_m)
        self.err_distances_filt.append(sqrt(self.x_m**2 + self.y_m**2 + self.altitude_m**2))
        self.err_times_filt.append(time.time())
        self.altitude_m_filt.append(altitude_m)
        
        #Reset average values
        self.x_m_avg = 0
        self.y_m_avg = 0
        self.altitude_m_avg = 0
        weight_total = 0

        #Calculate the weighted average of the errors
        for idx in range(len(self.x_m_filt)):
            time_diff = time.time() - self.err_times_filt[idx]
            weight_time = exp(-time_diff)
            weight_dist = 10-self.err_distances_filt[idx]
            if weight_dist < 2:
                weight_dist = 2
            weight_result = weight_time*weight_dist
            weight_total += weight_result
            self.x_m_avg += self.x_m_filt[idx]*weight_result
            self.y_m_avg += self.y_m_filt[idx]*weight_result
            self.altitude_m_avg += self.altitude_m_filt[idx]*weight_result
        self.x_m_avg = self.x_m_avg/weight_total
        self.y_m_avg = self.y_m_avg/weight_total
        self.altitude_m_avg = self.altitude_m_avg/weight_total

        #Calculate the target lat and lon using the avg value
        # self.target_lat, self.target_lon = calculate_new_coordinate(uav_lat, uav_lon, [self.x_m_avg, self.y_m_avg], heading)

        #Calculate the error in pixels using the avg value
        curr_err_px = transform_ground_to_img_xy((self.x_m_avg, self.y_m_avg), self.altitude_m_avg, cam_fov_hv, image_size)
        self.err_px_x = curr_err_px[0]
        self.err_px_y = curr_err_px[1]
        self.time_last_detection = rospy.Time.now().to_sec()

        
    
    def clear_errors(self):
        """Clears all errors from list (used when drone moved to new point)"""
        self.x_m_filt = []
        self.y_m_filt = []
        self.err_distances_filt = []
        self.err_times_filt = []
        self.altitude_m_filt = []

        self.x_m_avg = 0
        self.y_m_avg = 0
        self.altitude_m_avg = 0
        self.err_px_x = 0
        self.err_px_y = 0

    def check_for_timeout(self):
        """Check if the time since last detection of an object is more than 3s"""
        # if rospy.Time.now().to_sec() - self.time_last_detection > 300:################################change later
        #     return True
        return False


class uav(state):
    def __init__(self):
        state.__init__(self)
        self.altitude = None #Altitude of the fc in meters
        self.heading = 0 #Heading of the UAV in radians
        self.latitude = 29.183972 #Current of the UAV
        self.longitude = -81.043251 #Current longitude of the UAV
        self.state = self.descend_square
        #self.state = self.initial
        self.cam_hfov = 65*np.pi/180 #Input by user
        self.cam_vfov = 52*np.pi/180 #Input by user
        self.image_size = [640, 480] #Automatically updated by the code

class target_parameters:
    def __init__(self):
        #Change these values if the size of the target changes
        self.diameter_big = 0.84
        self.diameter_small = 0.29
        self.canny_max_threshold = 40 #param1 of hough circle transform
        self.hough_circle_detect_thr = 35 #param2 of hough circle transform
        self.factor = self.diameter_big/self.diameter_small #How much bigger the big circle is compared to the small circle (diameter)
        self.tin_diameter = 0.084 #Diameter of the tins in meters
        self.size_square = 2 #Size of the square in meters

# class waypoints:
#     def __init__(self):
#         self.waypoints = []


class tin_colours:
    def __init__(self):
        #Change this if the colour of the tins changes (hue value from 0 to 180)
        self.green_hue = 95
        self.blue_hue = 105
        self.red_hue = 170

class main():
    def __init__(self):
        # Initialize ROS node and necessary variables
        rospy.init_node("UAV_landing", anonymous=True)

        self.px4_state = State()  # PX4 state variable (Loiter, Offboard, etc.)
        self.target_pose = PoseStamped()  # Target pose variable
        self.Bridge = CvBridge()  # OpenCV bridge for image conversion
        self.err_estimation = error_estimation()  # Error estimation object
        self.uav_inst = uav()  # UAV instance object
        self.target_parameters_obj = target_parameters()  # Target parameters object
        self.tin_colours_obj = tin_colours()  # Tin colours object
        self.state_inst = state()  # State instance object
        self.waypoint_pose = PoseStamped()  # Waypoint pose variable
        self.final_vel = TwistStamped()  # Final velocity variable
        self.rate = rospy.Rate(60)  # ROS rate for loop frequency
        self.cv_image = None  # OpenCV image variable
        self.target_reached_time = None  # Time when target is reached
        self.current_pose = None  # Current pose variable
        self.angle = (0, 0, 0)  # Angle variable
        self.initialization_time = rospy.Time.now().to_sec()  # Initialization time
        self.armed_time = None
        self.offboard_switch_time = 0  # Offboard switch time
        self.loiter_switch_time = 0  # Loiter switch time
        self.waypoint_pushed = False  # Flag to indicate if waypoint is pushed
        self.radius_of_proximity = np.Inf  # Radius of proximity variable
        self.updated_img = False  # Flag to indicate if image is updated
        self.descend_altitude = None  # Descend altitude variable
        self.ascending_start_time = None  # Ascending start time
        self.ascended_by_m = 0  # Ascended by meters
        self.takeoff_pos = [0, 0, 0]  # Takeoff position in m (NED)
        self.manual_control = False
        self.takeoff_reached_time = None
        self.last_good_alignment_time = None
        self.well_aligned_time = 0


        self.flight_altitude = 10

        #####Subscribers#####
        # Subscribe to the downward depth camera image topic
        rospy.Subscriber("/rpi/rgb/image_raw/compressed", CompressedImage, callback=self.camera)
        # Subscribe to the local position topic
        rospy.Subscriber("/mavros/local_position/pose", PoseStamped, callback=self.current_position)
        # Subscribe to the MAVROS state topic
        rospy.Subscriber('mavros/state', State, callback=self.monitor_state)
        
        #####Waiting for services#####
        # Wait for the set_mode service to become available
        #rospy.wait_for_service('/mavros/set_mode')
        #self.set_mode = rospy.ServiceProxy('/mavros/set_mode', SetMode)
        # Wait for the arm service to become available
        #rospy.wait_for_service('/mavros/cmd/arming')
        #self.arm = rospy.ServiceProxy('/mavros/cmd/arming', CommandBool)
        
        #####Publishers#####
        # Create a publisher for the waypoint pose topic
        self.waypoint_pose_pub = rospy.Publisher("/mavros/setpoint_position/local", PoseStamped, queue_size=3)
        # Create a publisher for the landing camera image topic
        self.img_pub = rospy.Publisher("/rpi/rgb/image_raw/landing/rgb/image_raw/compressed", CompressedImage, queue_size=3)
        # Create a publisher for the velocity topic
        self.velocity_pub = rospy.Publisher("mavros/setpoint_velocity/cmd_vel", TwistStamped, queue_size=3)
        
        # Define the waypoints for the UAV to fly to (relative to map frame)
        # self.waypoints = [[-10, 40, 10.0],
        #           [4, 30.0, 10.0]]
        self.waypoints = [[-11, 36,  self.flight_altitude],
                  [4, 30.0,  self.flight_altitude]]
        self.waypoints_adjusted = False


        # Set the frame ID and timestamp for the waypoint pose
        self.waypoint_pose.header.frame_id = 'map'
        self.waypoint_pose.header.stamp = rospy.Time.now()

        # Calculate the orientation based on the angle variable (always point north)
        a = (np.pi/2)
        self.theta = np.arctan2(np.sin(a),np.cos(a))
        self.orientation = quaternion_from_euler(self.angle[0],self.angle[1],self.theta)

        # Set the orientation in the waypoint pose
        self.waypoint_pose.pose.orientation.x = self.orientation[0]
        self.waypoint_pose.pose.orientation.y = self.orientation[1]
        self.waypoint_pose.pose.orientation.z = self.orientation[2]
        self.waypoint_pose.pose.orientation.w = self.orientation[3]

        # Set the frame ID for the final velocity
        self.final_vel.header.frame_id = "map"

        # Initialize the start time for the check_for_time function
        check_for_time.start_time = None
        
        self.err_estimation.altitude_m_avg = 6###################
        self.err_estimation.time_last_detection = rospy.Time.now().to_sec()
        print("Initialization complete")

        # Run the landing process in a loop until rospy is shutdown
        while not rospy.is_shutdown():
            self.landing()  # Call the landing function
            self.rate.sleep()  # Sleep to maintain the desired loop frequency

    def current_position(self,msg):
        """Callback function for the current position subscriber updating vehicle position"""
        # Update current pose and altitude
        self.current_pose = msg
        self.uav_inst.altitude = self.current_pose.pose.position.z - self.takeoff_pos[2]
        # Calculate Euler angles from quaternion
        self.angle = euler_from_quaternion([msg.pose.orientation.x,msg.pose.orientation.y,msg.pose.orientation.z,msg.pose.orientation.w])

    def camera(self,msg):
        """Callback function for the camera subscriber updating the image"""
        ###using updated image makes sure that the image being used is not overwritten
        # Convert compressed image message to OpenCV format
        self.cv_image_updated = self.Bridge.compressed_imgmsg_to_cv2(msg,"bgr8")
        # Resize the image to a specific size
        self.cv_image_updated = cv.resize(self.cv_image_updated, (640, 480))
        self.cv_image_updated = cv.rotate(self.cv_image_updated, cv.ROTATE_180) #Camera is rotate by 180 degrees
        # Update the image size attribute
        self.uav_inst.image_size = (self.cv_image_updated.shape[1], self.cv_image_updated.shape[0])
        # Set the flag to indicate that the image has been updated
        self.updated_img = True

    def monitor_state(self,msg):
        # Update the PX4 state
        self.px4_state = msg

    def guided_descend(self, hover=False):
            """Guided descend function to align the UAV with the target and descend to the target position"""
            self.ascended_by_m = 0
            self.ascending_start_time = None

            # Calculate the distance between the estimated position and the target position
            deltaS = np.sqrt(np.square(self.err_estimation.x_m_avg) + np.square(self.err_estimation.y_m_avg))
            # Calculate the angle between the estimated position and the target position
            theta_horizontal = np.arctan2(self.err_estimation.y_m_avg, self.err_estimation.x_m_avg) #Angle to the target in x-y plane

            theta_vertical = np.arctan2(deltaS, self.err_estimation.altitude_m_avg) #Angle to the target in relative to straight down plane

            # Calculate the linear velocity based on the distance
            gain = 0.3
            self.linear_vel = gain * deltaS
            self.linear_vel = 0.8 if self.linear_vel > 0.8 else self.linear_vel #Avoid going too fast to be safe was 0.5
            ##################################
            desired_heading = np.pi/2
            current_heading = self.angle[2]
            gain_heading = 0.2
            angular_z_vel = gain_heading*(desired_heading-current_heading)
            self.final_vel.twist.angular.z = angular_z_vel

            # Publish the correction position if the distance is greater than 0.2
            if hover:
                rospy.loginfo("Hovering and aligning")
                #P controller to maintain altitude
                altitude_target = 1 #Hover at 1 m
                delta_alt = altitude_target - self.err_estimation.altitude_m_avg
                gain_alt = 0.8
                self.final_vel.twist.linear.z = gain_alt*delta_alt

                
                # Set the linear velocity components in the x and y directions
                self.final_vel.header.stamp = rospy.Time.now()
                self.final_vel.twist.linear.x = self.linear_vel * np.cos(theta_horizontal)
                self.final_vel.twist.linear.y = self.linear_vel * np.sin(theta_horizontal)
                
            elif theta_vertical > 8*np.pi/180: #Not well aligned in z (more than 10 deg off) or in final aligning stage
                rospy.loginfo("Aligning")
                
                # Set the linear velocity components in the x and y directions
                self.final_vel.header.stamp = rospy.Time.now()
                self.final_vel.twist.linear.x = self.linear_vel * np.cos(theta_horizontal)
                self.final_vel.twist.linear.y = self.linear_vel * np.sin(theta_horizontal)
                self.final_vel.twist.linear.z = 0
            else:
                rospy.loginfo("Descending")
                # Set the linear velocity components in the x and y directions
                self.final_vel.header.stamp = rospy.Time.now()
                self.final_vel.twist.linear.x = self.linear_vel * np.cos(theta_horizontal)
                self.final_vel.twist.linear.y = self.linear_vel * np.sin(theta_horizontal)
                self.final_vel.twist.linear.z = -0.2 #Descend with 0.2 m/s
                # Publish the velocity message
            self.velocity_pub.publish(self.final_vel)

    def slight_ascend(self):
        """Function to ascend slightly if the target is lost"""
        rospy.loginfo("Lost target. Ascending slightly.")
        if self.ascending_start_time is None: #Store time at which ascending started
            self.ascending_start_time = rospy.Time.now().to_sec()

        self.final_vel.header.stamp = rospy.Time.now()
        self.final_vel.twist.linear.x = 0
        self.final_vel.twist.linear.y = 0
        self.final_vel.twist.linear.z = 0.25 #Ascend with 0.25 m/s
        self.velocity_pub.publish(self.final_vel)
        #Calculate how much the UAV has ascended by based on the velocity and time elapsed
        self.ascended_by_m = (rospy.Time.now().to_sec() - self.ascending_start_time) * self.final_vel.twist.linear.z
        


    def landing(self):    
        if self.current_pose != None and self.waypoint_pose != None: #Check if the current pose and waypoint pose are not None
            # Check the current state of the UAV
            ########################################
            if self.uav_inst.state == self.state_inst.initial and (rospy.Time.now().to_sec() - self.initialization_time) > 2:
                # Arm the UAV manually
                rospy.loginfo_once("Vehicle is ready to armed...")
                if self.px4_state.armed:
                    rospy.loginfo_throttle(1,"Altitude: {}".format(self.current_pose.pose.position.z-self.takeoff_pos[2] ))
                    if self.armed_time is None:
                        #Set this time only the first time the vehicle is armed
                        self.armed_time = rospy.Time.now().to_sec()

                    if self.px4_state.mode == 'AUTO.LOITER':
                        #If the vehicle is at the desired altitude and correct mode, switch to fly to waypoint mode
                        rospy.loginfo_once("Takeoff alt reached")
                        # if self.takeoff_reached_time is None:
                        #     self.takeoff_reached_time = rospy.Time.now().to_sec()
                        # elif self.takeoff_reached_time - rospy.Time.now().to_sec() > 2:
                            # rospy.loginfo("Switching to waypoint")
                        self.uav_inst.state = self.state_inst.fly_to_waypoint
                        self.takeoff_reached_time = rospy.Time.now().to_sec()
                    elif self.px4_state.mode != 'AUTO.TAKEOFF' and (rospy.Time.now().to_sec() - self.armed_time) > 2:
                        #Make sure the vehicle is in takeoff mode (exept is already switched to loiter)
                        self.takeoff_pos = [self.current_pose.pose.position.x, self.current_pose.pose.position.y, self.current_pose.pose.position.z]
                        if self.waypoints_adjusted == False:
                            #Adjust the waypoints for the takeoff position
                            for waypoint in self.waypoints:
                                waypoint[0] = waypoint[0] + self.takeoff_pos[0]
                                waypoint[1] = waypoint[1] + self.takeoff_pos[1]
                                waypoint[2] = waypoint[2] + self.takeoff_pos[2]
                            self.waypoints_adjusted = True
                        mode = self.set_mode(custom_mode='AUTO.TAKEOFF')
                        if mode.mode_sent:
                            rospy.loginfo_once("Vehicle is taking off.")
                    
            ########################################
            elif self.uav_inst.state == self.state_inst.fly_to_waypoint and (rospy.Time.now().to_sec()-self.takeoff_reached_time)>2:
                rospy.loginfo_once("Flying to waypoint")
                if not self.waypoint_pushed:
                    #Push the waypoint to the UAV if not already done so
                    if len(self.waypoints) != 0:  
                        self.waypoint_pose.pose.position.x = self.waypoints[-1][0]
                        self.waypoint_pose.pose.position.y = self.waypoints[-1][1]
                        self.waypoint_pose.pose.position.z = self.waypoints[-1][2]
                        self.waypoints.pop()
                        self.waypoint_pushed = True
                    else:
                        pass
                
                # Publish the waypoint pose
                #rospy.loginfo("publishing towaypoint")
                self.waypoint_pose_pub.publish(self.waypoint_pose)
                
                # Calculate the delta x and delta y between the current pose and the waypoint pose
                delta_x = (self.waypoint_pose.pose.position.x - self.current_pose.pose.position.x)
                delta_y = (self.waypoint_pose.pose.position.y - self.current_pose.pose.position.y)
                delta_z = (self.waypoint_pose.pose.position.z - self.current_pose.pose.position.z)
                rospy.loginfo_throttle(1, "Delta x: {}, Delta y: {}, Delta z: {}".format(delta_x, delta_y, delta_z))
                
                # Calculate the radius of proximity
                self.radius_of_proximity = np.sqrt(delta_x**2 + delta_y**2 + delta_z**2)
                
                # Calculate the time since the offboard switch
                time_passed_offboard = rospy.Time.now().to_sec() - self.offboard_switch_time
                
                # Check if enough time has passed since the offboard switch
                if time_passed_offboard > 2:
                    # Check if the vehicle is not already in OFFBOARD mode
                    if self.px4_state.mode != 'OFFBOARD':
                        # Switch the vehicle to OFFBOARD mode
                        mode = self.set_mode(custom_mode='OFFBOARD')
                        #Reset the time passed to not check right again as the subscriber frequency for the mode is low
                        self.offboard_switch_time = rospy.Time.now().to_sec() 
                        if mode.mode_sent:
                            rospy.loginfo("Waypoint received: x = {}, delta x = {}, y = {}, delta y = {}, z = {}.\n"
                                          .format(self.waypoint_pose.pose.position.x, delta_x, self.waypoint_pose.pose.position.y, 
                                                  delta_y, self.waypoint_pose.pose.position.z))
                            rospy.loginfo("Vehicle is in OFFBOARD mode. Vehicle now flying to the waypoint.")
                
                # Check if the radius of proximity is less than or equal to 0.4
                if self.radius_of_proximity <= 0.4: ########################
                    # if self.px4_state.mode != 'AUTO.LOITER':    
                    # print("setting to loiter mode")
                    # # Switch the vehicle to AUTO.LOITER mode
                    # mode = self.set_mode(custom_mode='AUTO.LOITER')
                    # if mode.mode_sent:
                    #         rospy.loginfo("Vehicle is now in LOITER mode.")
                    self.target_reached_time = rospy.Time.now().to_sec()
                    rospy.loginfo_once("Reached the waypoint")
                    self.uav_inst.state = self.state_inst.detect_square
            #print("updated img: ", self.updated_img)  
            if self.updated_img:    #Only run if new image is available   
                self.cv_image = self.cv_image_updated #Update image to the new image      
                ########################################################
                if self.uav_inst.state == self.state_inst.detect_square and (rospy.Time.now().to_sec() - self.target_reached_time) > 5:
                    rospy.loginfo_throttle(3, "Detecting square")
                    #rospy.loginfo("publishing detect")
                    self.waypoint_pose_pub.publish(self.waypoint_pose)
                    # Check if square is detected within a certain duration
                    square_detected_err, is_bimodal, threshold_img = check_for_time(frame=self.cv_image, altitude=self.uav_inst.altitude, duration=5,
                                                                                    ratio_detected=0.5, size_square=self.target_parameters_obj.size_square, cam_hfov=self.uav_inst.cam_hfov)
                    # Merge the threshold image with the original image for debugging
                    debugging_frame = cv.hconcat([self.cv_image, cv.cvtColor(threshold_img, cv.COLOR_GRAY2BGR)])
                    self.cv_image = display_error_and_text(debugging_frame, (0, 0), self.err_estimation.altitude_m_avg, self.uav_inst)
                    # Add some text overlay to the image
                    

                    if square_detected_err is None:
                        pass  # Time not over yet
                        #print("Time not over yet")
                    elif square_detected_err is False:
                        # Square not detected, switch to flying to next waypoint
                        self.uav_inst.state = self.state_inst.fly_to_waypoint
                        self.waypoint_pushed = False
                        self.offboard_switch_time = rospy.Time.now().to_sec()
                        check_for_time.start_time = None
                        rospy.loginfo("Square not detected. Flying to next waypoint.")
                    else:  # Done and detected
                        check_for_time.start_time = None
                        if is_bimodal:
                            rospy.loginfo("Bimodal distribution detected")
                            # If multiple squares are detected, update errors for each square
                            for idx in range(2):
                                current_err = square_detected_err[idx-1]  # Reverse order so that closer waypoint is last in list
                                alt = self.current_pose.pose.position.z - self.takeoff_pos[2]
                                self.err_estimation.update_errors(current_err[0], current_err[1], alt, [self.uav_inst.cam_hfov, self.uav_inst.cam_vfov], self.uav_inst.image_size, self.angle[2])
                                self.err_estimation.clear_errors()  # Clear the errors so that the average is not used (two different platforms)

                                # Append waypoints with updated error estimation
                                self.waypoints.append([self.current_pose.pose.position.x + self.err_estimation.x_m,
                                                       self.current_pose.pose.position.y + self.err_estimation.y_m,
                                                        self.flight_altitude+self.takeoff_pos[2]])

                        else:
                            # If only one square is detected, update errors and append waypoint
                            alt = self.current_pose.pose.position.z - self.takeoff_pos[2]
                            self.err_estimation.update_errors(square_detected_err[0], square_detected_err[1], alt, [self.uav_inst.cam_hfov, self.uav_inst.cam_vfov], self.uav_inst.image_size, self.angle[2])
                            self.waypoints.append([self.current_pose.pose.position.x + self.err_estimation.x_m,
                                                   self.current_pose.pose.position.y + self.err_estimation.y_m,
                                                    self.flight_altitude+self.takeoff_pos[2]])

                        error_in_m = sqrt(self.err_estimation.x_m**2 + self.err_estimation.y_m**2)
                        print("current error: ", error_in_m)
                        if error_in_m < 2:
                            # If error is within threshold, switch to descending state
                            self.uav_inst.state = self.state_inst.descend_square
                            self.waypoints.pop()
                            self.err_estimation.altitude_m_avg = self.current_pose.pose.position.z - self.takeoff_pos[2]
                            rospy.loginfo("Target detected and now descending")
                        else:
                            # If error is too far, switch to flying to next waypoint
                            rospy.loginfo("Target detected but too far away. Flying closer. Distance: " + str(error_in_m) + "m")
                            self.uav_inst.state = self.state_inst.fly_to_waypoint
                            self.waypoint_pushed = False
                            self.offboard_switch_time = rospy.Time.now().to_sec()
                elif self.uav_inst.state == self.state_inst.detect_square:
                    #make sure it does not timeout
                    self.waypoint_pose_pub.publish(self.waypoint_pose)

                ########################################################
                elif self.uav_inst.state == self.state_inst.descend_square:
                    rospy.loginfo_throttle(3, "Descend square")
                    if self.err_estimation.altitude_m_avg is not None: #Make sure altitude is actually set
                        # Detect the square and update errors, altitude and the threshold image
                        current_alt = self.err_estimation.altitude_m_avg #+ self.ascended_by_m
                        error_xy, self.descend_altitude, threshold_img = detect_square_main(self.cv_image, current_alt, self.target_parameters_obj.size_square, self.uav_inst.cam_hfov)
                        # rospy.loginfo(self.current_pose.pose.position.z)
                        # Merge the threshold image with the original image for debugging
                        debugging_frame = cv.hconcat([self.cv_image, cv.cvtColor(threshold_img, cv.COLOR_GRAY2BGR)])
                        if error_xy != None: #Target detected
                            # Update the errors using the weighted running average filter
                            self.err_estimation.update_errors(error_xy[0][0], error_xy[0][1], self.descend_altitude, [self.uav_inst.cam_hfov, self.uav_inst.cam_vfov], self.uav_inst.image_size, self.angle[2])
                            
                            self.guided_descend()
                           
                            # Check if the vehicle is in OFFBOARD mode and switch to OFFBOARD mode if necessary
                            #if self.px4_state.mode != 'OFFBOARD':
                                #mode = self.set_mode(custom_mode='OFFBOARD')

                            if(self.err_estimation.altitude_m_avg != 0 and self.err_estimation.altitude_m_avg < 4):
                                self.uav_inst.state = self.state_inst.descend_concentric
                                rospy.loginfo("Descending using concentric circles")
                                    

                        elif self.err_estimation.check_for_timeout(): #No target detected for X s
                            rospy.loginfo_once("Timeout")
                            self.uav_inst.state = self.state_inst.return_to_launch
                        elif rospy.Time.now().to_sec() - self.err_estimation.time_last_detection > 0.2: #Target not detected but was detected recently
                            # Set the linear velocity components in the x, y, and z directions
                            self.slight_ascend()
                            """ if self.px4_state.mode != 'AUTO.LAND':    
                                mode = self.set_mode(custom_mode='AUTO.LAND')
                                if mode.mode_sent:
                                    rospy.loginfo_once("Target out of FOV. Vehicle will just land at the current position.") """

                        self.cv_image = display_error_and_text(debugging_frame, (self.err_estimation.err_px_x, self.err_estimation.err_px_y), self.err_estimation.altitude_m_avg,self.uav_inst)
                        
                    
                ########################################################
                elif self.uav_inst.state == self.state_inst.descend_concentric:
                    current_alt = self.err_estimation.altitude_m_avg + self.ascended_by_m
                    alt, error_xy, edges = concentric_circles(frame=self.cv_image, altitude=current_alt, cam_hfov=self.uav_inst.cam_hfov, circle_parameters_obj=self.target_parameters_obj) 
                    debugging_frame = cv.hconcat([self.cv_image, cv.cvtColor(edges, cv.COLOR_GRAY2BGR)])
                    if alt is not None: #Target detected
                        self.err_estimation.update_errors(error_xy[0], error_xy[1], alt, [self.uav_inst.cam_hfov, self.uav_inst.cam_vfov], self.uav_inst.image_size,self.angle[2])
                        self.guided_descend()
                    elif self.err_estimation.check_for_timeout(): #No target detected for 3s
                        print("Timeout")
                        self.uav_inst.state = self.state_inst.return_to_launch
                    elif rospy.Time.now().to_sec() - self.err_estimation.time_last_detection > 0.2: #Target not detected but was detected recently
                        self.slight_ascend()

                    if self.err_estimation.altitude_m_avg < 1.4:
                        self.uav_inst.state = self.state_inst.descend_inner_circle
                    self.cv_image = display_error_and_text(debugging_frame, (self.err_estimation.err_px_x, self.err_estimation.err_px_y), self.err_estimation.altitude_m_avg,self.uav_inst)
                    
                ########################################################
                elif self.uav_inst.state == self.state_inst.descend_inner_circle:
                    current_alt = self.err_estimation.altitude_m_avg + self.ascended_by_m
                    alt,error_xy, edges = small_circle(frame=self.cv_image, altitude=current_alt, cam_hfov=self.uav_inst.cam_hfov, circle_parameters_obj=self.target_parameters_obj)
                    debugging_frame = cv.hconcat([self.cv_image, cv.cvtColor(edges, cv.COLOR_GRAY2BGR)])

                    if alt is not None:
                        self.err_estimation.update_errors(error_xy[0], error_xy[1], alt, [self.uav_inst.cam_hfov, self.uav_inst.cam_vfov], self.uav_inst.image_size,self.angle[2])
                        if self.err_estimation.altitude_m_avg < 1: #Too low to descend further (risk of losing target)
                            self.guided_descend(hover=True) #Do not descend further align at current altitude
                            self.uav_inst.state = self.state_inst.align_before_landing
                        else:
                            self.guided_descend()
                    elif self.err_estimation.check_for_timeout(): #No target detected for longer time
                        print("Timeout")
                        self.uav_inst.state = self.state_inst.return_to_launch
                    elif rospy.Time.now().to_sec() - self.err_estimation.time_last_detection > 0.2: #Target not detected but was detected recently
                        self.slight_ascend()
                    self.cv_image = display_error_and_text(debugging_frame, (self.err_estimation.err_px_x, self.err_estimation.err_px_y), self.err_estimation.altitude_m_avg,self.uav_inst)

                ########################################################
                elif self.uav_inst.state == self.state_inst.align_before_landing:
                    #Align the UAV with the target right before landing
                    rospy.loginfo_throttle(1, "Aligning before landing")
                    current_alt = self.err_estimation.altitude_m_avg
                    alt,error_xy, edges = small_circle(frame=self.cv_image, altitude=current_alt, cam_hfov=self.uav_inst.cam_hfov, circle_parameters_obj=self.target_parameters_obj)
                    debugging_frame = cv.hconcat([self.cv_image, cv.cvtColor(edges, cv.COLOR_GRAY2BGR)])

                    if alt is not None:
                        self.err_estimation.update_errors(error_xy[0], error_xy[1], alt, [self.uav_inst.cam_hfov, self.uav_inst.cam_vfov], self.uav_inst.image_size,self.angle[2])
                        self.guided_descend(hover=True) #Do not descend further align at current altitude
                        distance = sqrt(self.err_estimation.x_m_avg**2 + self.err_estimation.y_m_avg**2)
                        if distance < 0.1: #Always less than 10cm of target
                            if self.well_aligned_time > 1: #UAV has been well aligned for 1s land now
                                self.uav_inst.state = self.state_inst.land
                                
                            if self.last_good_alignment_time is None:
                                self.last_good_alignment_time = rospy.Time.now().to_sec()
                            else:
                                self.well_aligned_time += rospy.Time.now().to_sec() - self.last_good_alignment_time
                                self.last_good_alignment_time = rospy.Time.now().to_sec()
                        else:
                            self.last_good_alignment_time = None
                            self.well_aligned_time = 0

                    elif self.err_estimation.check_for_timeout(): #No target detected for longer time
                        print("Timeout")
                        self.uav_inst.state = self.state_inst.return_to_launch
                    elif rospy.Time.now().to_sec() - self.err_estimation.time_last_detection > 0.5: #Target not detected but was detected recently (Ascend slightly and try again)
                        self.state_inst.descend_inner_circle #Change state to try again
                    self.cv_image = display_error_and_text(debugging_frame, (self.err_estimation.err_px_x, self.err_estimation.err_px_y), self.err_estimation.altitude_m_avg,self.uav_inst)


                ########################################################
                elif self.uav_inst.state == self.state_inst.land:
                    if self.px4_state.mode != 'AUTO.LAND':    
                        mode = self.set_mode(custom_mode='AUTO.LAND')
                        if mode.mode_sent:
                            rospy.loginfo_once("Vehicle is now landing.")
                
                elif self.uav_inst.state == self.state_inst.return_to_launch:
                    # For now just loiter no rtl
                    rospy.loginfo("Timeout. Loitering")
                    if self.px4_state.mode != 'AUTO.LOITER':    
                        mode = self.set_mode(custom_mode='AUTO.LOITER')
                        if mode.mode_sent:
                            rospy.loginfo_once("Vehicle is loitering and waiting (timeout)")

                
                
                #Set updated_to false to wait for new img
                self.updated_img = False 

                img_to_msg = self.Bridge.cv2_to_compressed_imgmsg(self.cv_image,'jpg')
                self.img_pub.publish(img_to_msg)
                # cv.imshow("Display",self.cv_image)
                # cv.waitKey(0)




      

if __name__ == "__main__":
    main()
