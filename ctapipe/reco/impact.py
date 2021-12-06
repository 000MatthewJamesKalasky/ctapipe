#!/usr/bin/env python3
"""

"""
import math
from string import Template
import copy

import numpy as np
import numpy.ma as ma
from astropy import units as u
from astropy.coordinates import SkyCoord, AltAz
from iminuit import Minuit
from scipy.stats import norm

from ctapipe.coordinates import (
    CameraFrame,
    NominalFrame,
    TiltedGroundFrame,
    GroundFrame,
    project_to_ground,
)

from ctapipe.containers import (
    ReconstructedGeometryContainer,
    ReconstructedEnergyContainer,
)
from ctapipe.reco.reco_algorithms import Reconstructor
from ctapipe.utils.template_network_interpolator import (
    TemplateNetworkInterpolator,
    TimeGradientInterpolator,
    DummyTemplateInterpolator,
    DummyTimeInterpolator
)
from ctapipe.reco.impact_utilities import *
from ctapipe.image.pixel_likelihood import neg_log_likelihood_approx, mean_poisson_likelihood_gaussian
from ctapipe.image.cleaning import dilate

from ctapipe.core import Provenance
PROV = Provenance()

__all__ = ["ImPACTReconstructor"]


class ImPACTReconstructor(Reconstructor):
    """This class is an implementation if the impact_reco Monte Carlo
    Template based image fitting method from parsons14.  This method uses a
    comparision of the predicted image from a library of image
    templates to perform a maximum likelihood fit for the shower axis,
    energy and height of maximum.

    Because this application is computationally intensive the usual
    advice to use astropy units for all quantities is ignored (as
    these slow down some computations), instead units within the class
    are fixed:

    - Angular units in radians
    - Distance units in metres
    - Energy units in TeV

    References
    ----------
    .. [parsons14] Parsons & Hinton, Astroparticle Physics 56 (2014), pp. 26-34

    """

    # For likelihood calculation we need the with of the
    # pedestal distribution for each pixel
    # currently this is not availible from the calibration,
    # so for now lets hard code it in a dict
    ped_table = {
        "LSTCam": 2.8,
        "NectarCam": 2.3,
        "FlashCam": 2.3,
        "CHEC": 0.5,
        "ASTRICam": 0.5,
        "DUMMY": 0,
        "UNKNOWN-960PX": 1.,

    }
    spe = 0.5  # Also hard code single p.e. distribution width

    def __init__(
        self,
        subarray,
        root_dir=".",
        minimiser="minuit",
        prior="",
        template_scale=1.0,
        xmax_offset=0,
        use_time_gradient=False,
        dummy_reconstructor=False
    ):
        """
        Create a new instance of ImPACTReconstructor
        """

        self.subarray = subarray
        # First we create a dictionary of image template interpolators
        # for each telescope type
        self.root_dir = root_dir
        self.priors = prior
        self.minimiser_name = minimiser

        # String templates for loading ImPACT templates
        self.amplitude_template = Template('${base}/${camera}.template.gz')
        self.time_template = Template('${base}/${camera}_time.template.gz')

        # We also need a conversion function from height above ground to
        # depth of maximum To do this we need the conversion table from CORSIKA
        (
            self.thickness_profile,
            self.altitude_profile,
        ) = get_atmosphere_profile("./atmosphere.ecsv")#get_atmosphere_profile_functions("paranal", with_units=False)

        # Next we need the position, area and amplitude from each pixel in the event
        # making this a class member makes passing them around much easier

        self.pixel_x, self.pixel_y = None, None
        self.image, self.time = None, None

        self.tel_types, self.tel_id = None, None

        # We also need telescope positions
        self.tel_pos_x, self.tel_pos_y = None, None

        # And the peak of the images
        self.peak_x, self.peak_y, self.peak_amp = None, None, None
        self.hillas_parameters, self.ped = None, None

        self.prediction = dict()
        self.time_prediction = dict()

        self.array_direction = None
        self.nominal_frame = None

        # For now these factors are required to fix problems in templates
        self.template_scale = template_scale
        self.scale_factor = None
        self.xmax_offset = xmax_offset
        self.use_time_gradient = use_time_gradient

        self.min = None
        self.dummy_reconstructor = dummy_reconstructor

    def __call__(self, event):
        """
        Perform the full shower geometry reconstruction on the input event.

        Parameters
        ----------
        event : container
            `ctapipe.containers.ArrayEventContainer`
        """

        hillas_dict = {}
        for tel_id, dl1 in event.dl1.tel.items():
            hillas = dl1.parameters.hillas
            if hillas is not None:
                if np.isfinite(dl1.parameters.hillas.intensity) and dl1.parameters.hillas.intensity>0:
                    hillas_dict[tel_id] = hillas


        # Due to tracking the pointing of the array will never be a constant
        array_pointing = SkyCoord(
            az=event.pointing.array_azimuth,
            alt=event.pointing.array_altitude,
            frame=AltAz(),
        )

        # And the pointing direction of the telescopes may not be the same
        telescope_pointings = {
            tel_id: SkyCoord(
                alt=event.pointing.tel[tel_id].altitude,
                az=event.pointing.tel[tel_id].azimuth,
                frame=AltAz(),
            )
            for tel_id in hillas_dict.keys()
        }

        # Finally get the telescope images and and the selection masks
        mask_dict, image_dict = {}, {}
        for tel_id in hillas_dict.keys():
            image = event.dl1.tel[tel_id].image
            image_dict[tel_id] = image
            mask = event.dl1.tel[tel_id].image_mask

            # Dilate the images around the original cleaning to help the fit
            for i in range(2):
                mask = dilate(self.subarray.tel[tel_id].camera.geometry, mask)
            mask_dict[tel_id] = mask


        # This is a placeholder for proper energy reconstruction
        reconstructor_prediction = event.dl2.stereo.geometry["HillasIntersection"]

        shower_result, energy_result = self.predict(hillas_dict=hillas_dict, subarray=self.subarray, 
                                                    array_pointing=array_pointing, telescope_pointings=telescope_pointings,
                                                    image_dict=image_dict, mask_dict=mask_dict,
                                                    shower_seed=reconstructor_prediction)

        #print(energy_result.energy/event.simulation.shower.energy)
        event.dl2.stereo.geometry["ImPACTReconstructor"] = shower_result
        event.dl2.stereo.energy['ImPACTReconstructor'] = energy_result

    def initialise_templates(self, tel_type):
        """Check if templates for a given telescope type has been initialised
        and if not do it and add to the dictionary

        Parameters
        ----------
        tel_type: dictionary
            Dictionary of telescope types in event

        Returns
        -------
        boolean: Confirm initialisation

        """

        for t in tel_type:
            if tel_type[t] in self.prediction.keys() or tel_type[t] == "DUMMY":
                continue

            if self.dummy_reconstructor:
                self.prediction[tel_type[t]] = DummyTemplateInterpolator()
            else:
                filename = self.amplitude_template.substitute(base=self.root_dir, camera=tel_type[t])
                self.prediction[tel_type[t]] = TemplateNetworkInterpolator(filename,
                                                                            bounds=((-4.4, 1), (-1.5, 1.5)))
                PROV.add_input_file(filename, role="ImPACT Template file for " + tel_type[t])

            if self.use_time_gradient:
                if self.dummy_reconstructor:
                    self.time_prediction[tel_type[t]] = DummyTimeInterpolator()
                else:
                    filename = self.time_template.substitute(base=self.root_dir, camera=tel_type[t])
                    self.time_prediction[tel_type[t]] = TimeGradientInterpolator(filename)
                    PROV.add_input_file(filename, role="ImPACT Time Template file for " + tel_type[t])


        return True

    def get_hillas_mean(self):
        """This is a simple function to find the peak position of each image
        in an event which will be used later in the Xmax calculation. Peak is
        found by taking the average position of the n hottest pixels in the
        image.

        Parameters
        ----------

        Returns
        -------
            None

        """
        peak_x = np.zeros([len(self.pixel_x)])  # Create blank arrays for peaks
        # rather than a dict (faster)
        peak_y = np.zeros(peak_x.shape)
        peak_amp = np.zeros(peak_x.shape)

        # Loop over all tels to take weighted average of pixel
        # positions This loop could maybe be replaced by an array
        # operation by a numpy wizard
        # Maybe a vectorize?
        tel_num = 0

        for hillas in self.hillas_parameters:           
            peak_x[tel_num] = hillas.fov_lon.to(u.rad).value  # Fill up array
            peak_y[tel_num] = hillas.fov_lat.to(u.rad).value
            peak_amp[tel_num] = hillas.intensity
            #print(tel_num, peak_x[tel_num], peak_y[tel_num], peak_amp[tel_num])

            tel_num += 1

        self.peak_x = peak_x  # * unit # Add to class member
        self.peak_y = peak_y  # * unit
        self.peak_amp = peak_amp

    # This function would be useful elsewhere so probably be implemented in a
    # more general form
    def get_shower_max(self, source_x, source_y, core_x, core_y, zen):
        """Function to calculate the depth of shower maximum geometrically
        under the assumption that the shower maximum lies at the
        brightest point of the camera image.

        Parameters
        ----------
        source_x: float
            Event source position in nominal frame
        source_y: float
            Event source position in nominal frame
        core_x: float
            Event core position in telescope tilted frame
        core_y: float
            Event core position in telescope tilted frame
        zen: float
            Zenith angle of event

        Returns
        -------
        float: Depth of maximum of air shower

        """
        # Calculate displacement of image centroid from source position (in
        # rad)
        disp = np.sqrt((self.peak_x - source_x) ** 2 + (self.peak_y - source_y) ** 2)
        
        # Calculate impact parameter of the shower
        impact = np.sqrt(
            (self.tel_pos_x - core_x) ** 2 + (self.tel_pos_y - core_y) ** 2
        )
        # Distance above telescope is ratio of these two (small angle)

        height = impact / disp
        weight = np.power(self.peak_amp, 0.0)  # weight average by sqrt amplitude
        # sqrt may not be the best option...

        # Take weighted mean of estimates
        mean_height = np.sum(height * np.cos(zen) * weight) / np.sum(weight)
        # This value is height above telescope in the tilted system,
        # we should convert to height above ground
        #mean_height *= np.cos(zen)
        # Add on the height of the detector above sea level
        mean_height += 1835#2150

        if mean_height > 100000 or np.isnan(mean_height):
            mean_height = 100000

        # Lookup this height in the depth tables, the convert Hmax to Xmax
        x_max = self.thickness_profile(mean_height * u.m).to("g cm-2").value
        # Convert to slant depth
        x_max /= np.cos(zen)
        #print(disp, impact, height, x_max)

        return x_max + self.xmax_offset

    def image_prediction(self, tel_type, zenith, azimuth, energy, impact, x_max, pix_x, pix_y):
        """Creates predicted image for the specified pixels, interpolated
        from the template library.

        Parameters
        ----------
        tel_type: string
            Telescope type specifier
        energy: float
            Event energy (TeV)
        impact: float
            Impact diance of shower (metres)
        x_max: float
            Depth of shower maximum (num bins from expectation)
        pix_x: ndarray
            X coordinate of pixels
        pix_y: ndarray
            Y coordinate of pixels

        Returns
        -------
        ndarray: predicted amplitude for all pixels

        """
        return self.prediction[tel_type](zenith, azimuth, energy, impact, x_max, pix_x, pix_y)

    def predict_time(self, tel_type, energy, impact, x_max):
        """Creates predicted image for the specified pixels, interpolated
        from the template library.

        Parameters
        ----------
        tel_type: string
            Telescope type specifier
        energy: float
            Event energy (TeV)
        impact: float
            Impact diance of shower (metres)
        x_max: float
            Depth of shower maximum (num bins from expectation)

        Returns
        -------
        ndarray: predicted amplitude for all pixels

        """
        return self.time_prediction[tel_type](energy, impact, x_max)

    def get_likelihood(
        self,
        source_x,
        source_y,
        core_x,
        core_y,
        energy,
        x_max_scale,
        goodness_of_fit=False,
    ):
        """Get the likelihood that the image predicted at the given test
        position matches the camera image.

        Parameters
        ----------
        source_x: float
            Source position of shower in the nominal system (in deg)
        source_y: float
            Source position of shower in the nominal system (in deg)
        core_x: float
            Core position of shower in tilted telescope system (in m)
        core_y: float
            Core position of shower in tilted telescope system (in m)
        energy: float
            Shower energy (in TeV)
        x_max_scale: float
            Scaling factor applied to geometrically calculated Xmax
        goodness_of_fit: boolean
            Determines whether expected likelihood should be subtracted from result
        Returns
        -------
        float: Likelihood the model represents the camera image at this position

        """
        # First we add units back onto everything.  Currently not
        # handled very well, maybe in future we could just put
        # everything in the correct units when loading in the class
        # and ignore them from then on

        zenith = (np.pi / 2) - self.array_direction.alt.to(u.rad).value
        azimuth = self.array_direction.az.to(u.deg).value
        
        # Geometrically calculate the depth of maximum given this test position
        x_max = self.get_shower_max(source_x, source_y, core_x, core_y, zenith)
        x_max *= x_max_scale
        
        # Calculate expected Xmax given this energy
        x_max_exp = guess_shower_depth(energy)  # / np.cos(20*u.deg)

        # Convert to binning of Xmax
        x_max_bin = x_max - x_max_exp

        # Check for range
        if x_max_bin > 150:
            x_max_bin = 150
        if x_max_bin < -100:
            x_max_bin = -100

        # Calculate impact distance for all telescopes
        impact = np.sqrt(
            (self.tel_pos_x - core_x) ** 2 + (self.tel_pos_y - core_y) ** 2
        )
        # And the expected rotation angle
        phi = np.arctan2((self.tel_pos_y - core_y), (self.tel_pos_x - core_x)) * u.rad

        # Rotate and translate all pixels such that they match the
        # template orientation
        pix_x_rot, pix_y_rot = rotate_translate(
            self.pixel_y, self.pixel_x, source_y, source_x, -1 * phi
        )

        # In the interpolator class we can gain speed advantages by using masked arrays
        # so we need to make sure here everything is masked
        prediction = ma.zeros(self.image.shape)
        prediction.mask = ma.getmask(self.image)

        time_gradients = np.zeros((self.image.shape[0], 2))
        #print(zenith, azimuth, energy, impact, x_max_bin)
        # Loop over all telescope types and get prediction
        for tel_type in np.unique(self.tel_types).tolist():
            type_mask = self.tel_types == tel_type

            prediction[type_mask] = self.image_prediction(
                tel_type,
                np.rad2deg(zenith), azimuth,
                energy * np.ones_like(impact[type_mask]),
                impact[type_mask],
                x_max_bin * np.ones_like(impact[type_mask]),
                np.rad2deg(pix_x_rot[type_mask]),
                np.rad2deg(pix_y_rot[type_mask]),
            )

            if self.use_time_gradient:
                time_gradients[type_mask] = self.predict_time(
                    tel_type,
                    energy * np.ones_like(impact[type_mask]),
                    impact[type_mask],
                    x_max_bin * np.ones_like(impact[type_mask]),
                )

        if self.use_time_gradient:
            time_mask = np.logical_and(np.invert(ma.getmask(self.image)), self.time > 0)
            weight = np.sqrt(self.image) * time_mask
            rv = norm()

            sx = pix_x_rot * weight
            sxx = pix_x_rot * pix_x_rot * weight

            sy = self.time * weight
            sxy = self.time * pix_x_rot * weight
            d = weight.sum(axis=1) * sxx.sum(axis=1) - sx.sum(axis=1) * sx.sum(axis=1)
            time_fit = (
                weight.sum(axis=1) * sxy.sum(axis=1) - sx.sum(axis=1) * sy.sum(axis=1)
            ) / d
            time_fit /= -1 * (180 / math.pi)
            chi2 = -2 * np.log(
                rv.pdf((time_fit - time_gradients.T[0]) / time_gradients.T[1])
            )

        # Likelihood function will break if we find a NaN or a 0
        prediction[np.isnan(prediction)] = 1e-8
        prediction[prediction < 1e-8] = 1e-8
        prediction *= self.scale_factor[:, np.newaxis]

        # Get likelihood that the prediction matched the camera image
        mask =  ma.getmask(self.image)

        like = neg_log_likelihood_approx(self.image, prediction, self.spe, self.ped)
        like[mask] = 0
        like = np.sum(like)
        if goodness_of_fit:
            return like - mean_poisson_likelihood_gaussian(prediction, self.spe, self.ped)

        prior_pen = 0
        # Add prior penalities if we have them
        if "energy" in self.priors:
            prior_pen += energy_prior(energy, index=-1)
            print("here")
        if "xmax" in self.priors:
            prior_pen += xmax_prior(energy, x_max)

        final_sum = like
        if self.use_time_gradient:
            final_sum += chi2.sum()  # * np.sum(ma.getmask(self.image))

        return final_sum

    def get_likelihood_min(self, x):
        """Wrapper class around likelihood function for use with scipy
        minimisers

        Parameters
        ----------
        x: ndarray
            Array of minimisation parameters

        Returns
        -------
        float: Likelihood value of test position

        """
        val = self.get_likelihood(x[0], x[1], x[2], x[3], x[4], x[5])

        return val

    def set_event_properties(
        self,
        hillas_dict,
        image_dict,
        time_dict,
        mask_dict,
        subarray,
        array_pointing,
        telescope_pointing
    ):
        """The setter class is used to set the event properties within this
        class before minimisation can take place. This simply copies a
        bunch of useful properties to class members, so that we can
        use them later without passing all this information around.

        Parameters
        ----------
        hillas_dict: dict
            dictionary with telescope IDs as key and
            HillasParametersContainer instances as values
        image_dict: dict
            Amplitude of pixels in camera images
        time_dict: dict
            Time information per each pixel in camera images
        mask_dict: dict
            Event image masks
        subarray: dict
            Type of telescope
        array_pointing: SkyCoord[AltAz]
            Array pointing direction in the AltAz Frame
        telescope_pointing: SkyCoord[AltAz]
            Telescope pointing directions in the AltAz Frame
        Returns
        -------
        None

        """
        # First store these parameters in the class so we can use them
        # in minimisation For most values this is simply copying
        self.image = image_dict

        self.tel_pos_x = np.zeros(len(hillas_dict))
        self.tel_pos_y = np.zeros(len(hillas_dict))
        self.scale_factor = np.zeros(len(hillas_dict))

        self.ped = np.zeros(len(hillas_dict))
        self.tel_types, self.tel_id = list(), list()

        max_pix_x = 0
        px, py, pa, pt = list(), list(), list(), list()
        self.hillas_parameters = list()

        # Get telescope positions in tilted frame
        tilted_frame = TiltedGroundFrame(pointing_direction=array_pointing)
        ground_positions = subarray.tel_coords
        grd_coord = GroundFrame(
            x=ground_positions.x, y=ground_positions.y, z=ground_positions.z
        )

        self.array_direction = array_pointing
        self.nominal_frame = NominalFrame(origin=self.array_direction)

        tilt_coord = grd_coord.transform_to(tilted_frame)

        type_tel = {}
        # So here we must loop over the telescopes
        for tel_id, i in zip(hillas_dict, range(len(hillas_dict))):

            geometry = subarray.tel[tel_id].camera.geometry
            type = subarray.tel[tel_id].camera.camera_name
            type_tel[tel_id] = type

            mask = mask_dict[tel_id]

            focal_length = subarray.tel[tel_id].optics.equivalent_focal_length * 1.022
            camera_frame = CameraFrame(
                telescope_pointing=telescope_pointing[tel_id],
                focal_length=focal_length,
            )
            camera_coords = SkyCoord(x=geometry.pix_x[mask], y=geometry.pix_y[mask], frame=camera_frame)
            nominal_coords = camera_coords.transform_to(self.nominal_frame)

            px.append(nominal_coords.fov_lon.to(u.rad).value)
            if len(px[i]) > max_pix_x:
                max_pix_x = len(px[i])
            py.append(nominal_coords.fov_lat.to(u.rad).value)
            pa.append(image_dict[tel_id][mask])
            pt.append(time_dict[tel_id][mask])

            self.ped[i] = self.ped_table[type]
            self.tel_types.append(type)
            self.tel_id.append(tel_id)
            self.tel_pos_x[i] = tilt_coord[tel_id-1].x.to(u.m).value
            self.tel_pos_y[i] = tilt_coord[tel_id-1].y.to(u.m).value

            self.hillas_parameters.append(hillas_dict[tel_id])
            self.scale_factor[i] = self.template_scale[tel_id]

        # Most interesting stuff is now copied to the class, but to remove our requirement
        # for loops we must copy the pixel positions to an array with the length of the
        # largest image

        # First allocate everything
        shape = (len(hillas_dict), max_pix_x)
        self.pixel_x, self.pixel_y = ma.zeros(shape), ma.zeros(shape)
        self.image, self.time, self.ped, self.spe = (
            ma.zeros(shape),
            ma.zeros(shape),
            ma.zeros(shape),
            ma.zeros(shape),
        )
        self.tel_types = np.array(self.tel_types)

        # Copy everything into our masked arrays
        for i in range(len(hillas_dict)):
            array_len = len(px[i])
            self.pixel_x[i][:array_len] = px[i]
            self.pixel_y[i][:array_len] = py[i]
            self.image[i][:array_len] = pa[i]
            self.time[i][:array_len] = pt[i]
            self.ped[i][:array_len] = self.ped_table[self.tel_types[i]]
            self.spe[i][:array_len] = 0.5

        # Set the image mask
        mask = self.image == 0.0
        self.pixel_x[mask], self.pixel_y[mask] = ma.masked, ma.masked
        self.image[mask] = ma.masked
        self.time[mask] = ma.masked

        # Finally run some functions to get ready for the event
        self.get_hillas_mean()
        self.initialise_templates(type_tel)

    def predict(self, hillas_dict, subarray, array_pointing, telescope_pointings=None,
                image_dict=None, mask_dict=None, shower_seed=None, energy_seed=None):
        """Predict method for the ImPACT reconstructor.
        Used to calculate the reconstructed ImPACT shower geometry and energy.

        Parameters
        ----------
        shower_seed: ReconstructedShowerContainer
            Seed shower geometry to be used in the fit
        energy_seed: ReconstructedEnergyContainer
            Seed energy to be used in fit

        Returns
        -------
        ReconstructedShowerContainer, ReconstructedEnergyContainer:
        """
        if image_dict is None:
            raise EmptyImages("Images not passed to ImPACT reconstructor")

        self.set_event_properties(copy.deepcopy(hillas_dict), image_dict, image_dict, mask_dict, subarray, array_pointing,
                                  telescope_pointings)

        self.reset_interpolator()

        # Copy all of our seed parameters out of the shower objects
        # We need to convert the shower direction to the nominal system
        horizon_seed = SkyCoord(az=shower_seed.az, alt=shower_seed.alt, frame=AltAz())
        nominal_seed = horizon_seed.transform_to(self.nominal_frame)

        source_x = nominal_seed.fov_lon.to_value(u.rad)
        source_y = nominal_seed.fov_lat.to_value(u.rad)
        # And the core position to the tilted ground frame
        ground = GroundFrame(x=shower_seed.core_x, y=shower_seed.core_y, z=0 * u.m)
        tilted = ground.transform_to(
            TiltedGroundFrame(pointing_direction=self.array_direction)
        )
        tilt_x = tilted.x.to(u.m).value
        tilt_y = tilted.y.to(u.m).value
        zenith = 90 * u.deg - self.array_direction.alt

        energy = 0
        preminimise = True
        # If we have a seed energy we can skip the minimisation step
        if energy_seed is not None:
            energy = energy_seed.energy.value
            preminimise = False
        seed = create_seed(source_x, source_y,
                           tilt_x, tilt_y, 
                           energy)


        # Perform maximum likelihood fit
        fit_params, errors, like = self.minimise(params=seed[0],
                                                 step=seed[1],
                                                 limits=seed[2],
                                                 energy_preminimisation=preminimise)


        # Create a container class for reconstructed shower
        shower_result = ReconstructedGeometryContainer()

        # Convert the best fits direction and core to Horizon and ground systems and
        # copy to the shower container
        nominal = SkyCoord(
            fov_lon=fit_params[0] * u.rad,
            fov_lat=fit_params[1] * u.rad,
            frame=self.nominal_frame,
        )
        horizon = nominal.transform_to(AltAz())

        # Transform everything back to a useful system
        shower_result.alt, shower_result.az = horizon.alt, horizon.az
        tilted = TiltedGroundFrame(
            x=fit_params[2] * u.m,
            y=fit_params[3] * u.m,
            pointing_direction=self.array_direction,
        )
        ground = project_to_ground(tilted)

        shower_result.core_x = ground.x
        shower_result.core_y = ground.y

        shower_result.is_valid = True

        # Currently no errors not available to copy NaN
        shower_result.alt_uncert = np.nan
        shower_result.az_uncert = np.nan
        shower_result.core_uncert = np.nan

        # Copy reconstructed Xmax
        shower_result.h_max = fit_params[5] * self.get_shower_max(
            fit_params[0],
            fit_params[1],
            fit_params[2],
            fit_params[3],
            zenith.to(u.rad).value,
        )
        # this should be h_max, but is currently x_max
        shower_result.h_max *= np.cos(zenith)
        shower_result.h_max_uncert = errors[5] * shower_result.h_max

        shower_result.goodness_of_fit = like

        # Create a container class for reconstructed energy
        energy_result = ReconstructedEnergyContainer()
        # Fill with results
        energy_result.energy = fit_params[4] * u.TeV
        energy_result.energy_uncert = errors[4] * u.TeV
        energy_result.is_valid = True

        return shower_result, energy_result

    def minimise(self, params, step, limits, energy_preminimisation=True):
        """

        Parameters
        ----------
        params: ndarray
            Seed parameters for fit
        step: ndarray
            Initial step size in the fit
        limits: ndarray
            Fit bounds
        minimiser_name: str
            Name of minimisation method
        max_calls: int
            Maximum number of calls to minimiser
        Returns
        -------
        tuple: best fit parameters and errors
        """
        limits = np.asarray(limits)
        
        energy = params[4]
        xmax_scale = 1

        # In this case we perform a pre minimisation on the energy in the case that we have no seed
        # it takes a little extra time, but not too much

        if energy_preminimisation:
            likelihood = 1e9
            # Try a few different seed energies to be sure we get the right one
            for seed_energy in [0.03, 0.1, 1, 10, 100]:
                self.min = Minuit(
                    self.get_likelihood,
                    source_x=params[0],source_y=params[1],
                    core_x=params[2],core_y=params[3],
                    energy=seed_energy,x_max_scale=params[5],
                    goodness_of_fit=False,
                )

                # Fix everything but the energy
                self.min.fixed = [True, True, True, True, False, True, True]
                self.min.errors = [0,0,0,0,seed_energy*0.1,0,0]
                self.min.limits = [[-1,1],[-1,1], [-1000,1000], [-1000,1000],
                                    [0.01, 500],[0.5,1.5], [False, False]]

                # Set loose limits as we only need rough numbers
                self.min.errordef = 1.0
                self.min.tol *= 1000
                self.min.strategy = 0

                migrad = self.min.migrad(iterate=1)
                fit_params = self.min.values

                # Only use if our value is better that the previous ones
                if migrad.fval < likelihood:
                    energy = fit_params["energy"]
                    limits[4] = [energy*0.1, energy*2.]

        # Now do the minimisation proper
        self.min = Minuit(
            self.get_likelihood,
            source_x=params[0], source_y=params[1],
            core_x=params[2], core_y=params[3],
            energy=energy, x_max_scale=xmax_scale,
            goodness_of_fit=False,
        )
        # This time leave everything free
        self.min.fixed = [False, False, False, False, False, False, True]

        self.min.errors = step
        self.min.limits = limits
        self.min.errordef = 1.0
        
        # Tighter fit tolerances
        self.min.tol *= 1000
        self.min.strategy = 1

        # Fit and output parameters and errors
        migrad = self.min.migrad(iterate=1)
        fit_params = self.min.values
        errors = self.min.errors
        return (
            (
                fit_params["source_x"], fit_params["source_y"],
                fit_params["core_x"], fit_params["core_y"],
                fit_params["energy"], fit_params["x_max_scale"],
            ),
            (
                errors["source_x"], errors["source_y"],
                errors["core_x"], errors["core_x"],
                errors["energy"], errors["x_max_scale"],
            ),
            self.min.fval,
        )

    def reset_interpolator(self):
        """
        This function is needed in order to reset some variables in the interpolator
        at each new event. Without this reset, a new event starts with information
        from the previous event.
        """
        for key in self.prediction:
            self.prediction[key].reset()
