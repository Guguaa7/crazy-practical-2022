import logging
import time

import cflib.crtp
from cflib.crazyflie import Crazyflie
from cflib.crazyflie.log import LogConfig
from cflib.crazyflie.syncCrazyflie import SyncCrazyflie
from cflib.crazyflie.syncLogger import SyncLogger
from cflib.positioning.motion_commander import MotionCommander
import numpy as np

logging.basicConfig(level=logging.ERROR)

######################################### INFO ###########################################

"""
ATTENTION : J'AI CHANGE LE SIGNE DE Y DANS LA CALLBACK ET A LA FIN DE LA STATE MACHINE QUAND
ON SET LA COMMANDE DE VITESSE POUR QUE LE SENS DES Y POSITIFS
CORRESPONDE AU SENS DU DESSIN DU PLAYGROUND -> SI CA POSE PROBLEME ON POURRA CHANGER
MAIS POUR LA PARTIE RECHERCHE DE PLATEFORME CA M'ARRANGAIT
"""


###################################### CONTROLLER ########################################

class P_controller:
    def __init__(self, kp=1, MAX_SPEED=0.1):
        self.kp = kp
        self.MAX_SPEED = MAX_SPEED
        self.u = np.array([0, 0, 0]) # command

    def get_u(self, pt2go, actual_pos):
        # Compute the error
        error = pt2go - actual_pos

        # Compute the Proportionnal command
        self.u = self.kp * error

        # Saturate the command
        for i in range(3):
            if self.u[i] > MAX_SPEED:
                self.u[i] = MAX_SPEED

            elif self.u[i] < -MAX_SPEED:
                self.u[i] = -MAX_SPEED

        return self.u

###################################### PLAYGROUND ########################################

class playground:
    def __init__(self):
        """        
                            W
        ##########################################
        #                   L               h    #
        # l   ------------------------------   l #
        #                                   |    #
        #                                   | H  #
        #                                   |    #
        #     ------------------------------     # H3
        #    |                                   #
        #    | H                                 #
        #    |            L                      #
        #  l  ------------------------------   l #
        #                                  h     #
        ##########################################
        #                                        #
        #                          xxxx          #
        #                          xxxx          #
        #                          xxxx          #
        #        xxxx                            # H2
        #        xxxx                            #
        #        xxxx                            #
        #                                        #
        ##########################################
        #                                        #
        #                                        #
        #                                        #
        #             ooooo                      #
   x0 > #             ooooo                      # H1
        #             ooooo                      #
        ^                                        #
        |                                        #
        O-->######################################
                        ^
                        y0
        """
        self.W = 3 # m
        self.H1 = 1.5 # m
        self.H2 = 2 # m
        self.H3 = 1.5 # m

        self.padHeight = 0.1
        self.padMargin = 0.01
        self.padEdge = np.zeros((4, 2))
        self.padCenter = np.array([0., 0.])

        #self.xyz0 = np.array([1, 0.4, 0.1]) # Inital position of the platform
        self.xyz0 = np.array([0.3, 0.7, 0.1])
###################################### CHARLES AIRLINES ########################################

class Charles:
    def __init__(self):

        print("Bienvenue sur Charles Airline")
        self.uri = "radio://0/80/2M/E7E7E7E7E7"
        self.default_height = 0.3

        self.playground = playground()

        # Attributes for moving to landing zone
        self.border = False

        # State machine obstacle avoidance while searching
        self.move = 0
        self.avoiding = False
        self.obs_y = 0.0
        #self.current_waypoint = [0.0, 0.0]

        #Right - forward - left
        #self.waypoints_tests = [[0.0, 2.0], [0.5, 2.0], [0.5, 0.0]]

        #Left - forward - right
        self.waypoints_tests = [[0.0, -1.5], [0.5, -1.5], [0.5, 0.0]]

        #Left
        #self.waypoints_tests = [[0.0, -2.0]]
        
        # Initial position in the global frame
        self.xyz0 = self.playground.xyz0
        

        # Position in the "take off platform" frame
        self.xyz = np.array([0., 0., 0.])
        self.xyz_old = np.array([0., 0., 0.])
        self.diff_xyz = np.array([0., 0., 0.])
        self.rpy = np.array([0., 0., 0.])
        self.vz = 0.
        self.az = 0.

        self.speed_controller = P_controller()
        
        # Position in the global frame
        self.xyz_global = self.xyz0
            
        # self.range = [front, back, up, left, right, zrange]
        self.range = np.array([0, 0, 0, 0, 0, 0])
        self.xyz_rate_cmd = np.array([0, 0, 0])
        self.rpy_rate_cmd = np.array([0, 0, 0])

        self.state = 0

        # variable related to landing pad
        self.stateCentering = 0
        self.edgeDetected = False
        self.edgeFound = 0  # 0:not found, 1:rising edge, 2:falling edge
        self.edgeThresholdUp = 0.012
        self.edgeThresholdDown = 0.007
        self.edgeTime = 0.
        self.edgeTimeDelay = 1.5
        self.centerReached = False
        self.idx = 0
        self.queueZ = 50 * [0.]
        self.minZ = float('inf')
        self.maxZ = float('-inf')
        self.diffZ = 0.
        self.varPlot = [[], []]

        self.pos_var_list = ['stateEstimate.x',
                             'stateEstimate.y',
                             'stateEstimate.z',
                             'stateEstimate.vz',
                             'stateEstimate.az']
        # 'stabilizer.roll',
        # 'stabilizer.pitch',
        # 'stabilizer.yaw']

        self.multi_var_list = ['range.front',
                               'range.back',
                               'range.up',
                               'range.left',
                               'range.right',
                               'range.zrange']

        # Searching path variables
        self.waypoints = None

        # Constants :
        self.min_dist = 300  # Distance to stop flying

        self.l = 0.1  # marge de chaque côté en y
        self.L = self.playground.W - 2 * self.l  # Largeur des allers retours en y
        self.h = 0.1  # marge de chaque côté en x
        self.N = 5  # Nombre d'allers
        self.H = (self.playground.H3 - 2 * self.h) / (self.N - 1)  # Ecart x entre chaque aller

        self.Te_loop = 0.01  # Cadence la boucle principale EN SECONDES
        self.Te_log = 10  # Cadence la réception des données EN !!! MILLISECONDES !!!

        print("Driver initialisation ..")
        cflib.crtp.init_drivers()

        print("Log Configuration ..")
        self.setLog()

    # ----------------------------------------------------------------------------------------#

    def is_not_close(self):
        # False if an object is too close to the drone (up)
        return (self.range[2] > self.min_dist)

    # ----------------------------------------------------------------------------------------#

    def is_close_obs(self, range):  # if use of self.range, change scale to mm
        MIN_DISTANCE = 300  # mm

        if range is None:
            return False
        else:
            return range < MIN_DISTANCE

    # ----------------------------------------------------------------------------------------#

    def setLog(self):

        self.log_position = LogConfig(name='Position', period_in_ms=self.Te_log)
        self.log_multiranger = LogConfig(name='Multiranger', period_in_ms=self.Te_log)
        
        for var in self.pos_var_list:
            self.log_position.add_variable(var, 'float')

        for var in self.multi_var_list:
            self.log_multiranger.add_variable(var, 'float')

    # ----------------------------------------------------------------------------------------#

    def log_pos_callback(self, timestamp, data, logconf):
        # Get x,y,z and roll, pitch, yaw values and save it into self variables
        self.xyz = np.array([data[self.pos_var_list[0]], -data[self.pos_var_list[1]], data[self.pos_var_list[2]]])
        self.xyz_global = self.xyz + self.xyz0  # Position in the global frame
        self.diff_xyz = self.xyz - self.xyz_old
        self.queueZ.pop(0)
        self.queueZ.append(self.xyz[2] ** 3)
        # self.minZ = min(self.queueZ)
        # self.maxZ = max(self.queueZ)
        self.diffZ = max(self.queueZ) - min(self.queueZ)

        # self.rpy = np.array([data[self.pos_var_list[3]], data[self.pos_var_list[4]], data[self.pos_var_list[5]]])
        self.vz = data[self.pos_var_list[3]]
        self.az = data[self.pos_var_list[4]]

    # ----------------------------------------------------------------------------------------#

    def log_multi_callback(self, timestamp, data, logconf):
        # Get multiranger values and save it into self variables
        self.range = [data[self.multi_var_list[0]],
                      data[self.multi_var_list[1]],
                      data[self.multi_var_list[2]],
                      data[self.multi_var_list[3]],
                      data[self.multi_var_list[4]],
                      data[self.multi_var_list[5]]]

    # ----------------------------------------------------------------------------------------#

    def move_to_landing_zone(self):
        keep_flying = True

        # Some constants to modify
        VELOCITY = 0.3
        MIN_Y = 0.5
        MAX_DISTANCE = 1

        # Commands
        velocity_x = 0.0
        velocity_y = 0.0

        # There is an obstacle in front
        if self.is_close_obs(self.range[0]):  
            # print("Front : ", self.range[0])

            # If near the border, avoid by the right
            if ((self.xyz[1] + self.xyz0[1]) < MIN_Y) or (self.border):
                self.border = True
                velocity_x = 0.0
                velocity_y = 2 * VELOCITY

            # If not near the border, avoid by the left
            else:
                velocity_x = 0.0
                velocity_y = -2 * VELOCITY

        # If no obstacle, go forward
        else:  
            # print("Straight")
            velocity_x = VELOCITY
            velocity_y = 0.0

        # Arrived in searching zone
        if (self.xyz[0] > MAX_DISTANCE):
            keep_flying = False
            velocity_x = 0.0
            velocity_y = 0.0

        # Send command
        self.xyz_rate_cmd = [velocity_x, velocity_y, 0]

        # Return flase if in searching zone, true otherwise
        return keep_flying

    # ----------------------------------------------------------------------------------------#

    def set_waypoints(self):
        """
        Create a list of waypoints in the GLOBAL FRAME to search the platform
                            W
        ##########################################
        #                   L               h    #
        # l   ------------------------------   l #
        #                                   |    #
        #                                   | H  #
        #                                   |    #
        #     ------------------------------     # H3
        #    |                                   #
        #    | H                                 #
        #    |            L                      #
        #  l  ------pi-----------------------  l #
        #                                  h     #
        ##########################################
        """
        # When we enter this function, drone is at position pi

        self.waypoints = np.array([])

        # Start en bas à gauche : P(x0, l, 0.5) -> On assume que xyz_global[1] > W/2
        self.waypoints = np.append(self.waypoints, [self.xyz_global[0], self.l, self.default_height])
        # Direction to start obstacle avoidance
        self.move = 0

        for i in range(self.N - 1):
            self.waypoints = np.append(self.waypoints, self.waypoints[6 * i:6 * i + 3] + np.array([self.H, 0, 0]))
            self.waypoints = np.append(self.waypoints,
                                       self.waypoints[6 * i + 3:6 * i + 6] + np.array([0, self.L * (-1) ** i, 0]))

        # Correct starting direction
        if self.xyz_global[1] < self.playground.W / 2:
            # Direction to start obstacle avoidance
            self.move = 2
            # Mirroir + décalage de 2*l + L
            for i in range(int(len(self.waypoints)/3)):
                self.waypoints[3*i+1] = -self.waypoints[3*i+1] + 2*self.l + self.L
                

    # ----------------------------------------------------------------------------------------#

    def follow_waypoints(self):
        """ Follow the waypoints given in self.waypoints"""
        # Min distance to consider point as reached
        epsilon = 0.05 # m
        modulus_error = np.sum((self.waypoints[0:3] - self.xyz_global)**2) # Modulus of the error [m^2]
        
        # Check if the waypoint has been reached
        if modulus_error < epsilon ** 2:
            # If yes, check if it was the last waypoint in the list
            # print("Next Waypoint")
            if len(self.waypoints) == 3:
                # If yes stop the search
                self.waypoints = None
                
                return False
  
            # Otherwise remove the first waypoint from the list
            self.waypoints = self.waypoints[3:len(self.waypoints)]

        # Set current waypoint to reach
        current_waypoint = self.waypoints[0:3]

        # Compute speed rate command
        self.xyz_rate_cmd = self.speed_controller.get_u(current_waypoint, self.xyz_global)

        return True

#----------------------------------------------------------------------------------------#

    def obstacle_avoidance_searching(self, current_waypoint) :
    
        VELOCITY_X = 0.3
        VELOCITY_Y = 0.2 #0.5

        velocity_x = 0.0
        velocity_y = 0.0

        x_waypoint = current_waypoint[0]
        y_waypoint = current_waypoint[1]
        #print("x waypoint : ", x_waypoint)
        #print("y waypoint : ", y_waypoint)

        y_right = 0.3
        y_left = -0.5

        reached = False

        #Case right
        if self.move == 0:  
            print("Case right")
            if self.is_close_obs(self.range[4]) :
                #print("Obstacle in view")
                velocity_x = 2*VELOCITY_X
                velocity_y = 0
                self.obs_y = self.xyz[1]
                self.avoiding = True
                
            elif self.avoiding : 
                #print("Avoiding")
                if self.xyz[1] < (self.obs_y + 1.0) :
                    #print("Avoiding 2")
                    velocity_x = 0
                    velocity_y = VELOCITY_Y
                else :
                    self.avoiding = False

            elif (not self.is_close_obs(self.range[1]) and self.xyz[0] > (x_waypoint+0.1) and self.avoiding == False):
                print("Back to the trajectory")
                velocity_x = -VELOCITY_X
                velocity_y = 0
                

            else :
                #print("Straight")
                velocity_x = 0
                velocity_y = VELOCITY_Y

            if self.xyz[1] > y_waypoint :
                velocity_x = 0
                velocity_y = 0
                reached = True

        #Case forward
        if self.move == 1 :
            print("Case forward")
            if (self.is_close_obs(self.range[0]) and (self.xyz[1] >= 1.0)) : #A changer
                #print("I go left")
                velocity_y = -VELOCITY_Y
                velocity_x = 0
            
            elif (self.is_close_obs(self.range[0]) and (self.xyz[1] < 1.0)) : #A changer
                #print("I go right")
                velocity_x = 0
                velocity_y = VELOCITY_Y
            
            else :
                #print("I go forward")
                velocity_x = VELOCITY_X
                velocity_y = 0

            if self.xyz[0] > x_waypoint :
                #print("I'm at waypoint")
                velocity_x = 0
                velocity_y = 0
                reached = True
                

        #Case left
        if self.move == 2:   #Case side
            print("Case left")
            if self.is_close_obs(self.range[3]):
                #print("Obstacle in view")
                velocity_x = 2*VELOCITY_X
                velocity_y = 0
                self.obs_y = self.xyz[1]
                self.avoiding = True
                

            elif self.avoiding : 
                if self.xyz[1] > (self.obs_y - 1.0) :
                    #print("Avoiding")
                    velocity_x = 0
                    velocity_y = -VELOCITY_Y
                else :
                    self.avoiding = False

            elif (not self.is_close_obs(self.range[1]) and self.xyz[0] > (x_waypoint+0.1) and self.avoiding == False):
                print("Back to the trajectory")
                velocity_x = -VELOCITY_X
                velocity_y = 0
                

            else :
                #print("Straight")
                velocity_x = 0
                velocity_y = -VELOCITY_Y

            if self.xyz[1] < y_waypoint:
                #print("Reached")
                velocity_x = 0
                velocity_y = 0
                reached = True
                

        self.xyz_rate_cmd = [velocity_x, velocity_y, 0]
        return reached
#------------------------------------------------------------------------------------------#

    def back_to_start(self) :

        VELOCITY_X = 0.3
        VELOCITY_Y = 0.2 #0.5

        velocity_x = 0.0
        velocity_y = 0.0

        # x > 0
        if self.xyz[0] > 0 :
            # If obstacle behind
            if self.is_close_obs(self.range[1]):
                # If y > 0, avoid obstacle to left
                if self.xyz[1] > 0 :
                    velocity_x = 0.0
                    velocity_y = -VELOCITY_Y
                
                # If y < 0, avoid obstacle to right
                else :
                    velocity_x = 0.0
                    velocity_y = VELOCITY_Y
            else :
                velocity_x = -VELOCITY_X
                velocity_y = 0.0
                
        # If x = 0, move to y = 0
        else :
            # y > 0 -> go left while avoiding obstacle
            if self.xyz[1] > 0 :

                if self.is_close_obs(self.range[3]):
                    #print("Obstacle in view")
                    velocity_x = 2*VELOCITY_X
                    velocity_y = 0
                    self.obs_y = self.xyz[1]
                    self.avoiding = True
                

                elif self.avoiding : 
                    if self.xyz[1] > (self.obs_y - 1.0) :
                        #print("Avoiding")
                        velocity_x = 0
                        velocity_y = -VELOCITY_Y
                    else :
                        self.avoiding = False

                elif (not self.is_close_obs(self.range[1]) and (self.xyz[0] > 0.05) and self.avoiding == False):
                    #print("Back to the trajectory")
                    velocity_x = -2*VELOCITY_X
                    velocity_y = 0
                    

                else :
                    #print("Straight")
                    velocity_x = 0
                    velocity_y = -VELOCITY_Y

            # y < 0 : go right while avoiding obstacle
            else :
                if self.is_close_obs(self.range[4]) :
                    #print("Obstacle in view")
                    velocity_x = 2*VELOCITY_X
                    velocity_y = 0
                    self.obs_y = self.xyz[1]
                    self.avoiding = True
                
                elif self.avoiding : 
                    #print("Avoiding")
                    if self.xyz[1] < (self.obs_y + 1.0) :
                        #print("Avoiding 2")
                        velocity_x = 0
                        velocity_y = VELOCITY_Y
                    else :
                        self.avoiding = False

                elif (not self.is_close_obs(self.range[1]) and self.xyz[0] > 0.05 and self.avoiding == False):
                    #print("Back to the trajectory")
                    velocity_x = -2*VELOCITY_X
                    velocity_y = 0
                    

                else :
                    #print("Straight")
                    velocity_x = 0
                    velocity_y = VELOCITY_Y
                
        self.xyz_rate_cmd = [velocity_x, velocity_y, 0]
    
    # ----------------------------------------------------------------------------------------#
    def detectEdge(self, edgeType=0):
        #print("%.4f" % self.diffZ, "%.4f" % self.vz)
        self.edgeFound = 0
        if (self.diffZ > self.edgeThresholdUp) and not self.edgeDetected:
            self.edgeDetected = True

            if self.vz > 0.1:
                if (edgeType == 0 or edgeType == 1):
                    self.edgeFound = 1
                else:
                    self.edgeFound = -1
            else:
                if (edgeType == 0 or edgeType == 2):
                    self.edgeFound = 2
                else:
                    self.edgeFound = -2
            # print(self.edgeFound)

        elif (self.diffZ <= self.edgeThresholdDown) and self.edgeDetected:
            self.edgeDetected = False

    # ----------------------------------------------------------------------------------------#

    def centering(self):
        if self.stateCentering == 0:
            self.detectEdge()
            if self.edgeFound:  # first falling edge detected, go back
                if self.idx:
                    self.playground.padEdge[self.stateCentering, 0] = self.xyz_global[0]
                    self.playground.padEdge[self.stateCentering, 1] = self.xyz_global[1]
                    time.sleep(0.4)
                    self.xyz_rate_cmd *= -1

                    self.stateCentering += 1
                    self.idx = 0
                    # print('first edge')
                else:
                    self.idx = 1

        elif self.stateCentering == 1:
            self.detectEdge()
            if self.edgeFound:  # second falling edge detected, compute pseudo center and add to waypoint
                if self.idx:
                    self.playground.padEdge[self.stateCentering, 0] = self.xyz_global[0]
                    self.playground.padEdge[self.stateCentering, 1] = self.xyz_global[1]

                    self.playground.padCenter = [(self.playground.padEdge[0, 0] + self.playground.padEdge[1, 0]) / 2,
                                                 (self.playground.padEdge[0, 1] + self.playground.padEdge[1, 1]) / 2]
                    # print(self.playground.padCenter)
                    time.sleep(0.4)

                    self.waypoints = np.array([])
                    self.waypoints = np.append(self.waypoints,
                                               [self.playground.padCenter[0], self.playground.padCenter[1],
                                                self.default_height])

                    self.xyz_rate_cmd_old = self.xyz_rate_cmd  # for later use

                    self.stateCentering += 1
                    self.idx = 0
                    # print('second edge')
                    # print("Pad center = ", self.playground.padCenter)
                else:
                    self.idx = 1

        elif self.stateCentering == 2:
            self.detectEdge()
            if not self.centerReached:
                if not self.follow_waypoints():
                    self.centerReached = True
                    self.xyz_rate_cmd = np.array(
                        [self.xyz_rate_cmd_old[1], self.xyz_rate_cmd_old[0], self.xyz_rate_cmd_old[2]])
                    # print('pseudo center')
            else:
                if self.edgeFound:  # third falling edge detected, go back
                    self.playground.padEdge[self.stateCentering, 0] = self.xyz_global[0]
                    self.playground.padEdge[self.stateCentering, 1] = self.xyz_global[1]

                    self.centerReached = False

                    time.sleep(0.4)
                    self.xyz_rate_cmd *= -1

                    self.stateCentering += 1
                    # print('third edge')

        elif self.stateCentering == 3:
            self.detectEdge()
            if self.edgeFound:  # fourth (last) falling edge detected, compute center
                if self.idx:
                    self.playground.padEdge[self.stateCentering, 0] = self.xyz_global[0]
                    self.playground.padEdge[self.stateCentering, 1] = self.xyz_global[1]

                    if abs(self.xyz_rate_cmd[0]) > abs(self.xyz_rate_cmd[1]):
                        self.playground.padCenter[0] = (
                                    (self.playground.padEdge[2, 0] + self.playground.padEdge[3, 0]) / 2)
                    else:
                        self.playground.padCenter[1] = (
                                    (self.playground.padEdge[2, 1] + self.playground.padEdge[3, 1]) / 2)
                    # print(self.playground.padCenter)
                    time.sleep(0.4)
                    self.waypoints = np.array([])
                    self.waypoints = np.append(self.waypoints,
                                               [self.playground.padCenter[0], self.playground.padCenter[1],
                                                self.default_height])

                    self.stateCentering += 1
                    self.idx = 0
                    # print('fourth edge')
                else:
                    self.idx = 1

        elif self.stateCentering == 4:
            if not self.follow_waypoints():
                self.centerReached = True
                self.xyz_rate_cmd = [0, 0, 0]

    # ----------------------------------------------------------------------------------------#
    
    def stateMachine(self, scf):
        with MotionCommander(scf, default_height=self.default_height) as mc:
            while (self.is_not_close()):
                #print(self.range[2])

                if self.state == 0:

                    # ---- Take off ----#

                    # default height has been reached -> Next state
                    if self.xyz[2] >= self.default_height:
                        self.state += 1
                        
                        #self.state += 1
                        #print("Next state : " + str(self.state))

                elif self.state == 1:

                    #---- Fly to zone 2 ----#
                    
                    # self.range = [front, back, up, left, right, zrange]

                    keep_flying = self.move_to_landing_zone()
                    #keep_flying = False
                    
                    if not keep_flying:
                        print('Safe arrival in Landing zone ! Let the scan begin')
                        keep_searching = True
                        self.state += 1
                        # print("Next state : " + str(self.state))

                elif self.state == 2:

                    #---- Search landing zone ----#

                    if self.waypoints is None and keep_searching == True:
                        # Wait to compute waypoints (searching path)
                        self.xyz_rate_cmd = np.array([0, 0, 0])
                        self.set_waypoints()
                        print("Setting waypoints")
                        print(self.waypoints)
                    
                    change_waypoint = False

                    #change_waypoint = self.obstacle_avoidance_searching(self.waypoints[0])
                    # From global frame to drone frame
                    initial_pos = [self.xyz0[0], self.xyz0[1]]
                    waypoint_drone = [self.waypoints[0]-initial_pos[0], self.waypoints[1]-initial_pos[1]]
                    #print(waypoint_drone)
                    change_waypoint = self.obstacle_avoidance_searching(waypoint_drone)
                    

                    if change_waypoint :
                        #print("Pop")
                        self.avoiding = False
                        #np.delete(self.waypoints, [0,1,2])
                        self.waypoints = self.waypoints[3:len(self.waypoints)]
                        #waypoint_drone = self.waypoints[0]-initial_pos
                        waypoint_drone = [self.waypoints[0]-initial_pos[0], self.waypoints[1]-initial_pos[1]]

                        # If right or left before, forward now
                        if self.move != 1 :
                            self.move = 1

                        # If forward before, determine right or left frome y coordinate of next waypoint in drone frame
                        else :
                            if waypoint_drone[1] < 0 :
                                self.move = 2
                            else :
                                self.move = 0
                        
                    # Return true if we reached last waypoint, false otherwise
                    #keep_searching = self.follow_waypoints()
                    keep_searching = True

                    #####################################################################################3
                    # IS EDGE DETECTION BREAKING THE LOOP OF FOLLOWING WAYPOINTS ?
                    ######################################################################################3
                    self.detectEdge()

                    if not keep_searching:
                        self.state += 1
                        self.waypoints = None
                        self.xyz_rate_cmd = 0.1*np.sign(self.xyz_rate_cmd)*self.xyz_rate_cmd/max(abs(self.xyz_rate_cmd))
                        # print("Next state : " + str(self.state))

                elif self.state == 3:
                    # ---- Search center of the landing zone ----#
                    # self.detectEdge()
                    self.centering()

                    #---- Search center of the landing zone ----#

                    if self.centerReached and self.stateCentering == 4:
                        self.stateCentering = 0
                        self.state += 1
                        print('center reached')
                        # print("Next state : " + str(self.state))


                elif self.state == 4:
                    mc.land()
                    # time.sleep(5.)
                    # mc.take_off()
                    # self.state += 1
                    # print("Next state : " + str(self.state))

                    #if True:
                        #self.state = 0
                        #print("Next state : " + str(self.state))

                elif self.state == 5:
                #---- Back to start ---------------------#    
                    self.back_to_start()

                else:
                    print("Woooooops invalid state")
                    
                #print(self.xyz_rate_cmd[1])
                
                mc.start_linear_motion(self.xyz_rate_cmd[0], -self.xyz_rate_cmd[1], self.xyz_rate_cmd[2], self.rpy_rate_cmd[0])

                time.sleep(self.Te_loop)

    # ----------------------------------------------------------------------------------------#

    def run(self):
        print("Connection ..")

        with SyncCrazyflie(self.uri, cf=Crazyflie(rw_cache='./cache')) as scf:
            print("Charles connecté, Charles content")

            print("Add config ..")
            scf.cf.log.add_config(self.log_position)
            scf.cf.log.add_config(self.log_multiranger)
            print("Add Callback ..")
            self.log_position.data_received_cb.add_callback(self.log_pos_callback)
            self.log_multiranger.data_received_cb.add_callback(self.log_multi_callback)

            time.sleep(1)

            print("Start dataflow")
            self.log_position.start()
            self.log_multiranger.start()

            print("Z'eeeeeeest parti")
            self.stateMachine(scf)

            print("Goodbye :'(")
            self.log_position.stop()
            self.log_multiranger.stop()

####################################### MAIN ##############################################

test = Charles()
test.run()

