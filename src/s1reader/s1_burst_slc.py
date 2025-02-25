import os
from dataclasses import dataclass
import datetime
import tempfile
import warnings
from packaging import version

import isce3
import numpy as np
from osgeo import gdal

from s1reader import s1_annotation
from .s1_burst_id import S1BurstId


# Other functionalities
def polyfit(xin, yin, zin, azimuth_order, range_order,
            sig=None, snr=None, cond=1.0e-12,
            max_order=True):
    """
    Fit 2-D polynomial
    Parameters:
    xin: np.ndarray
       Array locations along x direction
    yin: np.ndarray
       Array locations along y direction
    zin: np.ndarray
       Array locations along z direction
    azimuth_order: int
       Azimuth polynomial order
    range_order: int
       Slant range polynomial order
    sig: -
       ---------------------------
    snr: float
       Signal to noise ratio
    cond: float
       ---------------------------
    max_order: bool
       ---------------------------

    Returns:
    poly: isce3.core.Poly2D
       class represents a polynomial function of range
       'x' and azimuth 'y'
    """
    x = np.array(xin)
    xmin = np.min(x)
    xnorm = np.max(x) - xmin
    if xnorm == 0:
        xnorm = 1.0
    x = (x - xmin) / xnorm

    y = np.array(yin)
    ymin = np.min(y)
    ynorm = np.max(y) - ymin
    if ynorm == 0:
        ynorm = 1.0
    y = (y - ymin) / ynorm

    z = np.array(zin)
    big_order = max(azimuth_order, range_order)

    arr_list = []
    for ii in range(azimuth_order + 1):
        yfact = np.power(y, ii)
        for jj in range(range_order + 1):
            xfact = np.power(x, jj) * yfact
            if max_order:
                if ((ii + jj) <= big_order):
                    arr_list.append(xfact.reshape((x.size, 1)))
            else:
                arr_list.append(xfact.reshape((x.size, 1)))

    A = np.hstack(arr_list)
    if sig is not None and snr is not None:
        raise Exception('Only one of sig / snr can be provided')
    if sig is not None:
        snr = 1.0 + 1.0 / sig
    if snr is not None:
        A = A / snr[:, None]
        z = z / snr

    val, res, _, _ = np.linalg.lstsq(A, z, rcond=cond)
    if len(res) > 0:
        print('Chi squared: %f' % (np.sqrt(res / (1.0 * len(z)))))
    else:
        print('No chi squared value....')
        print('Try reducing rank of polynomial.')

    coeffs = []
    count = 0
    for ii in range(azimuth_order + 1):
        row = []
        for jj in range(range_order + 1):
            if max_order:
                if (ii + jj) <= big_order:
                    row.append(val[count])
                    count = count + 1
                else:
                    row.append(0.0)
            else:
                row.append(val[count])
                count = count + 1
        coeffs.append(row)
    poly = isce3.core.Poly2d(coeffs, xmin, ymin, xnorm, ynorm)
    return poly

@dataclass
class AzimuthCarrierComponents:
    kt: np.ndarray
    eta: float
    eta_ref: float

    @property
    def antenna_steering_doppler(self):
        return self.kt * (self.eta - self.eta_ref)

    @property
    def carrier(self):
        return np.pi * self.kt * ((self.eta - self.eta_ref) ** 2)

@dataclass(frozen=True)
class Doppler:
    poly1d: isce3.core.Poly1d
    lut2d: isce3.core.LUT2d

@dataclass(frozen=True)
class Sentinel1BurstSlc:
    '''Raw values extracted from SAFE XML.
    '''
    #ipf_version:float
    ipf_version: version.Version
    sensing_start: datetime.datetime
    radar_center_frequency: float
    wavelength: float
    azimuth_steer_rate: float
    azimuth_time_interval: float
    slant_range_time: float
    starting_range: float
    iw2_mid_range: float
    range_sampling_rate: float
    range_pixel_spacing: float
    shape: tuple()
    azimuth_fm_rate: isce3.core.Poly1d
    doppler: Doppler
    range_bandwidth: float
    polarization: str # {VV, VH, HH, HV}
    burst_id: S1BurstId
    platform_id: str # S1{A,B}
    safe_filename: str # SAFE file name
    center: tuple # {center lon, center lat} in degrees
    border: list # list of lon, lat coordinate tuples (in degrees) representing burst border
    orbit: isce3.core.Orbit
    orbit_direction: str
    abs_orbit_number: int  # Absolute orbit number
    # VRT params
    tiff_path: str  # path to measurement tiff in SAFE/zip
    i_burst: int
    first_valid_sample: int
    last_valid_sample: int
    first_valid_line: int
    last_valid_line: int
    # window parameters
    range_window_type: str
    range_window_coefficient: float
    rank: int # The number of PRI between transmitted pulse and return echo.
    prf_raw_data: float  # Pulse repetition frequency (PRF) of the raw data [Hz]
    range_chirp_rate: float # Range chirp rate [Hz]

    # Correction information
    burst_calibration: s1_annotation.BurstCalibration  # Radiometric correction
    burst_noise: s1_annotation.BurstNoise  # Thermal noise correction
    burst_eap: s1_annotation.BurstEAP  # EAP correction

    def __str__(self):
        return f"Sentinel1BurstSlc: {self.burst_id} at {self.sensing_start}"

    def __repr__(self):
        return f"{self.__class__.__name__}(burst_id={self.burst_id})"

    def as_isce3_radargrid(self):
        '''Init and return isce3.product.RadarGridParameters.

        Returns:
        --------
        _ : RadarGridParameters
            RadarGridParameters constructed from class members.
        '''

        prf = 1 / self.azimuth_time_interval

        length, width = self.shape

        time_delta = datetime.timedelta(days=2)
        ref_epoch = isce3.core.DateTime(self.sensing_start - time_delta)
        # sensing start with respect to reference epoch
        sensing_start = time_delta.total_seconds()

        # init radar grid
        return isce3.product.RadarGridParameters(sensing_start,
                                                 self.wavelength,
                                                 prf,
                                                 self.starting_range,
                                                 self.range_pixel_spacing,
                                                 isce3.core.LookSide.Right,
                                                 length,
                                                 width,
                                                 ref_epoch)

    def slc_to_file(self, out_path: str, fmt: str = 'ENVI'):
        '''Write burst to GTiff file.

        Parameters:
        -----------
        out_path : string
            Path of output GTiff file.
        '''
        if not self.tiff_path:
            warn_str = f'Unable write SLC to file. Burst does not contain image data; only metadata.'
            warnings.warn(warn_str)
            return

        # get output directory of out_path
        dst_dir, _ = os.path.split(out_path)

        # create VRT; make temporary if output not VRT
        if fmt != 'VRT':
            temp_vrt = tempfile.NamedTemporaryFile(dir=dst_dir)
            vrt_fname = temp_vrt.name
        else:
            vrt_fname = out_path
        self.slc_to_vrt_file(vrt_fname)

        if fmt == 'VRT':
            return

        # open temporary VRT and translate to GTiff
        src_ds = gdal.Open(vrt_fname)
        gdal.Translate(out_path, src_ds, format=fmt)

        # clean up
        src_ds = None


    def slc_to_vrt_file(self, out_path):
        '''Write burst to VRT file.

        Parameters:
        -----------
        out_path : string
            Path of output VRT file.
        '''
        if not self.tiff_path:
            warn_str = 'Unable write SLC to file. Burst does not contain image data; only metadata.'
            warnings.warn(warn_str)
            return

        line_offset = self.i_burst * self.shape[0]

        inwidth = self.last_valid_sample - self.first_valid_sample + 1
        inlength = self.last_valid_line - self.first_valid_line + 1
        outlength, outwidth = self.shape
        yoffset = line_offset + self.first_valid_line
        localyoffset = self.first_valid_line
        xoffset = self.first_valid_sample
        gdal_obj = gdal.Open(self.tiff_path, gdal.GA_ReadOnly)
        fullwidth = gdal_obj.RasterXSize
        fulllength = gdal_obj.RasterYSize

        # TODO maybe cleaner to write with ElementTree
        tmpl = f'''<VRTDataset rasterXSize="{outwidth}" rasterYSize="{outlength}">
    <VRTRasterBand dataType="CFloat32" band="1">
        <NoDataValue>0.0</NoDataValue>
        <SimpleSource>
            <SourceFilename relativeToVRT="1">{self.tiff_path}</SourceFilename>
            <SourceBand>1</SourceBand>
            <SourceProperties RasterXSize="{fullwidth}" RasterYSize="{fulllength}" DataType="CInt16"/>
            <SrcRect xOff="{xoffset}" yOff="{yoffset}" xSize="{inwidth}" ySize="{inlength}"/>
            <DstRect xOff="{xoffset}" yOff="{localyoffset}" xSize="{inwidth}" ySize="{inlength}"/>
        </SimpleSource>
    </VRTRasterBand>
</VRTDataset>'''

        with open(out_path, 'w') as fid:
            fid.write(tmpl)

    def get_az_carrier_poly(self, offset=0.0, xstep=500, ystep=50,
                            az_order=5, rg_order=3, index_as_coord=False):
        """
        Estimate burst azimuth carrier polymonials
        Parameters
        ----------
        offset: float
            Offset between reference and secondary bursts
        xstep: int
            Spacing along x direction
        ystep: int
            Spacing along y direction
        az_order: int
            Azimuth polynomial order
        rg_order: int
            Slant range polynomial order
        index_as_coord: bool
            If true, polyfit with az/range indices. Else, polyfit with az/range.

        Returns
        -------
        poly: isce3.core.Poly2D
           class represents a polynomial function of range
           'x' and azimuth 'y'
        """

        rdr_grid = self.as_isce3_radargrid()

        lines, samples = self.shape
        x = np.arange(0, samples, xstep, dtype=int)
        y = np.arange(0, lines, ystep, dtype=int)
        x_mesh, y_mesh = np.meshgrid(x, y)

        # Estimate azimuth carrier
        az_carr_comp = self.az_carrier_components(
                                        offset=offset,
                                        position=(y_mesh, x_mesh))

        # Fit azimuth carrier polynomial with x/y or range/azimuth
        if index_as_coord:
            az_carrier_poly = polyfit(x_mesh.flatten()+1, y_mesh.flatten()+1,
                                      az_carr_comp.carrier.flatten(), az_order,
                                      rg_order)
        else:
            # Convert x/y to range/azimuth
            rg = self.starting_range + (x + 1) * self.range_pixel_spacing
            az = rdr_grid.sensing_start + (y + 1) * self.azimuth_time_interval
            rg_mesh, az_mesh = np.meshgrid(rg, az)

            # Estimate azimuth carrier polynomials
            az_carrier_poly = polyfit(rg_mesh.flatten(), az_mesh.flatten(),
                                  az_carr_comp.carrier.flatten(), az_order,
                                  rg_order)

        return az_carrier_poly

    def as_dict(self):
        """
        Return SLC class attributes as dict

        Returns
        -------
        self_as_dict: dict
           Dict representation as a dict
        """
        self_as_dict = {}
        for key, val in self.__dict__.items():
            if key == 'sensing_start':
                val = str(val)
            elif key == 'center':
                val = val.coords[0]
            elif isinstance(val, np.float64):
                val = float(val)
            elif key == 'azimuth_fm_rate':
                temp = {}
                temp['order'] = val.order
                temp['mean'] = val.mean
                temp['std'] = val.std
                temp['coeffs'] = val.coeffs
                val = temp
            elif key == 'burst_id':
                val = str(val)
            elif key == 'border':
                val = self.border[0].wkt
            elif key == 'doppler':
                temp = {}

                temp['poly1d'] = {}
                temp['poly1d']['order'] = val.poly1d.order
                temp['poly1d']['mean'] = val.poly1d.mean
                temp['poly1d']['std'] = val.poly1d.std
                temp['poly1d']['coeffs'] = val.poly1d.coeffs

                temp['lut2d'] = {}
                temp['lut2d']['x_start'] = val.lut2d.x_start
                temp['lut2d']['x_spacing'] = val.lut2d.x_spacing
                temp['lut2d']['y_start'] = val.lut2d.y_start
                temp['lut2d']['y_spacing'] = val.lut2d.y_spacing
                temp['lut2d']['length'] = val.lut2d.length
                temp['lut2d']['width'] = val.lut2d.width
                temp['lut2d']['data'] = val.lut2d.data.flatten().tolist()

                val = temp
            elif key == 'orbit':
                temp = {}
                temp['ref_epoch'] = str(val.reference_epoch)
                temp['time'] = {}
                temp['time']['first'] = val.time.first
                temp['time']['spacing'] = val.time.spacing
                temp['time']['last'] = val.time.last
                temp['time']['size'] = val.time.size
                temp['position_x'] = val.position[:,0].tolist()
                temp['position_y'] = val.position[:,1].tolist()
                temp['position_z'] = val.position[:,2].tolist()
                temp['velocity_x'] = val.velocity[:,0].tolist()
                temp['velocity_y'] = val.velocity[:,1].tolist()
                temp['velocity_z'] = val.velocity[:,2].tolist()
                val = temp
            self_as_dict[key] = val
        return self_as_dict


    def _steps_to_vecs(self, range_step, az_step):
        ''' convert range_step (meters) and az_step (seconds) into aranges to
        generate LUT2ds
        '''
        step_errs = []
        if range_step <= 0:
            step_errs.append('range')
        if az_step <= 0:
            step_errs.append('azimuth')
        if step_errs:
            step_errs = ', '.join(step_errs)
            err_str = f'Following step size(s) <=0: {step_errs}'
            raise ValueError(err_str)

        # container to store names of axis vectors that are invalid: i.e. size 0
        vec_errs = []

        # compute range vector
        n_range = np.ceil(self.width * self.range_pixel_spacing / range_step).astype(int)
        range_vec = self.starting_range + np.arange(0, n_range) * range_step
        if range_vec.size == 0:
            vec_errs.append('range')

        # compute azimuth vector
        n_az = np.ceil(self.length * self.azimuth_time_interval / az_step).astype(int)
        rdrgrid = self.as_isce3_radargrid()
        az_vec = rdrgrid.sensing_start + np.arange(0, n_az) * az_step
        if az_vec.size == 0:
            vec_errs.append('azimuth')

        if vec_errs:
            vec_errs = ', '.join(vec_errs)
            err_str = f'Cannot build aranges from following step(s): {vec_errs}'
            raise ValueError(err_str)

        return range_vec, az_vec


    def bistatic_delay(self, range_step=1, az_step=1):
        '''Computes the bistatic delay correction in azimuth direction
        due to the movement of the platform between pulse transmission and echo reception
        as described in equation (21) in Gisinger et al. (2021, TGRS).

        References
        -------
        Gisinger, C., Schubert, A., Breit, H., Garthwaite, M., Balss, U., Willberg, M., et al.
          (2021). In-Depth Verification of Sentinel-1 and TerraSAR-X Geolocation Accuracy Using
          the Australian Corner Reflector Array. IEEE Trans. Geosci. Remote Sens., 59(2), 1154-
          1181. doi:10.1109/TGRS.2019.2961248
        ETAD-DLR-DD-0008, Algorithm Technical Baseline Document. Available: https://sentinels.
          copernicus.eu/documents/247904/4629150/Sentinel-1-ETAD-Algorithm-Technical-Baseline-
          Document.pdf

        Parameters
        -------
        range_step : int
            Spacing along x/range direction [meters]
        az_step : int
            Spacing along y/azimuth direction [seconds]

        Returns
        -------
           LUT2D object of bistatic delay correction in seconds as a function
           of the azimuth time and slant range, or range and azimuth indices.
           This correction needs to be added to the SLC tagged azimuth time to
           get the corrected azimuth times.
        '''

        pri = 1.0 / self.prf_raw_data
        tau0 = self.rank * pri
        tau_mid = self.iw2_mid_range * 2.0 / isce3.core.speed_of_light

        slant_vec, az_vec = self._steps_to_vecs(range_step, az_step)

        tau = slant_vec * 2.0 / isce3.core.speed_of_light

        # the first term (tau_mid/2) is the bulk bistatic delay which was
        # removed from the orginial azimuth time by the ESA IPF. Based on
        # Gisinger et al. (2021) and ETAD ATBD, ESA IPF has used the mid of
        # the second subswath to compute the bulk bistatic delay. However
        # currently we have not been able to verify this from ESA documents.
        # This implementation follows the Gisinger et al. (2021) for now, we
        # can revise when we hear back from ESA folks.
        bistatic_correction_vec = tau_mid / 2 + tau / 2 - tau0
        ny = az_vec.size
        bistatic_correction = np.tile(bistatic_correction_vec.reshape(1,-1),
                                      (ny,1))

        return isce3.core.LUT2d(slant_vec, az_vec, bistatic_correction)

    def geometrical_and_steering_doppler(self, range_step=500, az_step=50):
        """
        Compute total Doppler which is the sum of two components:
        (1) the geometrical Doppler induced by the relative movement
        of the sensor and target
        (2) the TOPS specicifc Doppler caused by the electric steering
        of the beam along the azimuth direction resulting in Doppler varying
        with azimuth time.
        Parameters
        ----------
        range_step: int
            Spacing along x/range direction [meters]
        az_step: int
            Spacing along y/azimuth direction [seconds]

        Returns
        -------
           LUT2D object of total doppler in Hz as a function of the azimuth
           time and slant range, or range and azimuth indices.
           This correction needs to be added to the SLC tagged azimuth time to
           get the corrected azimuth times.
        """
        range_vec, az_vec = self._steps_to_vecs(range_step, az_step)

        # convert from meters to pixels
        x_vec = (range_vec - self.starting_range) / self.range_pixel_spacing

        # convert from seconds to pixels
        rdrgrid = self.as_isce3_radargrid()
        y_vec = (az_vec - rdrgrid.sensing_start) / self.azimuth_time_interval

        # compute az carrier components with pixels
        x_mesh, y_mesh = np.meshgrid(x_vec, y_vec)
        az_carr_comp = self.az_carrier_components(
                                        offset=0.0,
                                        position=(y_mesh, x_mesh))

        geometrical_doppler = self.doppler.poly1d.eval(range_vec)

        total_doppler = az_carr_comp.antenna_steering_doppler + geometrical_doppler

        return isce3.core.LUT2d(range_vec, az_vec, total_doppler)

    def doppler_induced_range_shift(self, range_step=500, az_step=50):
        """
        Computes the range delay caused by the Doppler shift as described
        by Gisinger et al 2021

        Parameters
        ----------
        range_step: int
            Spacing along x/range direction [meters]
        az_step: int
            Spacing along y/azimuth direction [seconds]

        Returns
        -------
        isce3.core.LUT2d:
           LUT2D object of range delay correction [seconds] as a function
           of the azimuth time and slant range, or x and y indices.

        """
        range_vec, az_vec = self._steps_to_vecs(range_step, az_step)

        doppler_shift = self.geometrical_and_steering_doppler(range_step=range_step,
                                                              az_step=az_step)
        tau_corr = doppler_shift.data / self.range_chirp_rate

        return isce3.core.LUT2d(range_vec, az_vec, tau_corr)

    def az_carrier_components(self, offset, position):
        '''
        Estimate azimuth carrier and store in numpy arrary. Also return
        contributing components.

        Parameters
        ----------
        offset: float
           Offset between reference and secondary burst
        position: tuple
           Tuple of locations along y and x directions in pixels

        Returns
        -------
        eta: float
            zero-Doppler azimuth time centered in the middle of the burst
        eta_ref: float
            refernce time
        kt: np.ndarray
            Doppler centroid rate in the focused TOPS SLC data [Hz/s]
        carr: np.ndarray
           Azimuth carrier

        Reference
        ---------
        https://sentinels.copernicus.eu/documents/247904/0/Sentinel-1-TOPS-SLC_Deramping/b041f20f-e820-46b7-a3ed-af36b8eb7fa0
        '''
        # Get self.sensing mid relative to orbit reference epoch
        fmt = "%Y-%m-%dT%H:%M:%S.%f"
        orbit_ref_epoch = datetime.datetime.strptime(self.orbit.reference_epoch.__str__()[:-3], fmt)

        t_mid = self.sensing_mid - orbit_ref_epoch
        _, v = self.orbit.interpolate(t_mid.total_seconds())
        vs = np.linalg.norm(v)
        ks = 2 * vs * self.azimuth_steer_rate / self.wavelength

        y, x = position

        n_lines, _ = self.shape
        eta = (y - (n_lines // 2) + offset) * self.azimuth_time_interval
        rng = self.starting_range + x * self.range_pixel_spacing

        f_etac = np.array(
            self.doppler.poly1d.eval(rng.flatten().tolist())).reshape(rng.shape)
        ka = np.array(
            self.azimuth_fm_rate.eval(rng.flatten().tolist())).reshape(rng.shape)

        eta_ref = (self.doppler.poly1d.eval(
            self.starting_range) / self.azimuth_fm_rate.eval(
            self.starting_range)) - (f_etac / ka)
        kt = ks / (1.0 - ks / ka)

        return AzimuthCarrierComponents(kt, eta, eta_ref)


    @property
    def sensing_mid(self):
        '''Returns sensing mid as datetime.datetime object.

        Returns:
        --------
        _ : datetime.datetime
            Sensing mid as datetime.datetime object.
        '''
        d_seconds = 0.5 * self.length * self.azimuth_time_interval
        return self.sensing_start + datetime.timedelta(seconds=d_seconds)

    @property
    def sensing_stop(self):
        '''Returns sensing end as datetime.datetime object.

        Returns:
        --------
        _ : datetime.datetime
            Sensing end as datetime.datetime object.
        '''
        d_seconds = (self.length - 1) * self.azimuth_time_interval
        return self.sensing_start + datetime.timedelta(seconds=d_seconds)

    @property
    def burst_duration(self):
        '''Returns burst sensing duration as float in seconds.

        Returns:
        --------
        _ : float
            Burst sensing duration as float in seconds.
        '''
        return self.azimuth_time_interval * self.length

    @property
    def length(self):
        return self.shape[0]

    @property
    def width(self):
        return self.shape[1]

    @property
    def swath_name(self):
        '''Swath name in iw1, iw2, iw3.'''
        return self.burst_id.subswath.lower()

    @property
    def thermal_noise_lut(self):
        '''
        Returns the LUT for thermal noise correction for the burst
        '''
        if self.burst_noise is None:
            raise ValueError('burst_noise is not defined for this burst.')

        return self.burst_noise.compute_thermal_noise_lut(self.shape)

    @property
    def eap_compensation_lut(self):
        '''Returns LUT for EAP compensation.

        Returns:
        -------
            _: Interpolated EAP gain for the burst's lines

        '''
        if self.burst_eap is None:
            raise ValueError('burst_eap is not defined for this burst.'
                            f' IPF version = {self.ipf_version}')

        return self.burst_eap.compute_eap_compensation_lut(self.width)

    @property
    def relative_orbit_number(self):
        '''Returns the relative orbit number of the burst.'''
        orbit_number_offset = 73 if self.platform_id == 'S1A' else 202
        return (self.abs_orbit_number - orbit_number_offset) % 175 + 1
