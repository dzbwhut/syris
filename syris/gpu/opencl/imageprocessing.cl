/*
 * Image processing routines on OpenCL.
 *
 * Requires definition of vfloat data type, which defines single or double
 * precision for floating point numbers. Also requires vcomplex data type
 * for working with complex numbers.
 */


/*
 * Fresnel approximated wavefield propagation.
 */
__kernel void propagator(__global vcomplex *out,
							const vfloat distance,
							const vfloat lam,
							const vfloat pixel_size,
							const vcomplex phase_factor) {

	int ix = get_global_id(0);
	int iy = get_global_id(1);
	int n = get_global_size(0);
	vfloat i, j, tmp;
	vcomplex result, c_tmp;

	/* Map image coordinates to fourier coordinates. */
	i = -0.5 + ((vfloat) ix) / n;
	j = -0.5 + ((vfloat) iy) / n;

	/*
	 * Fresnel propagator in the Fourier domain:
	 *
	 * F(i, j) = e^(2*pi*distance*i/lam) * e^(-i*pi*lam*distance*(i^2 + j^2)).
	 */
	tmp = - M_PI * lam * distance * (i * i + j * j) /
			(pixel_size * pixel_size);
	if (phase_factor.x == 0 and phase_factor.y == 0) {
		result = (vcomplex)(cos(tmp), sin(tmp));
	} else {
		c_tmp = (vcomplex)(cos(tmp), sin(tmp));
		result = vc_mul(&phase_factor, &c_tmp);
	}

	/* Lowest frequencies are in the corners. */
	out[n * ((iy + n / 2) % n) + ((ix + n / 2) % n)] = result;
}


/*
 * 2D normalized Gaussian in Fourier space.
 */
__kernel void gauss_2d_f(__global vcomplex *out,
						const float2 sigma,
						const vfloat pixel_size) {
    int ix = get_global_id(0);
    int iy = get_global_id(1);
    int n = get_global_size(0);
    vfloat i, j;

	/* Map image coordinates to fourier coordinates. */
	i = -0.5 + ((vfloat) ix) / n;
	j = -0.5 + ((vfloat) iy) / n;

    out[n * ((iy + n / 2) % n) + ((ix + n / 2) % n)] = (vcomplex)
    		(exp(- 2 * M_PI * M_PI * sigma.x * sigma.x * i * i /
    				(pixel_size * pixel_size) -
    				2 * M_PI * M_PI * sigma.y * sigma.y * j * j /
					(pixel_size * pixel_size)), 0);
}
