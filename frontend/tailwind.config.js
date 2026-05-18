/** @type {import('tailwindcss').Config} */
module.exports = {
    darkMode: ["class"],
    content: [
    "./src/**/*.{js,jsx,ts,tsx}",
    "./public/index.html"
  ],
  theme: {
  	extend: {
  		// Typography standard (2026-05-18, scale bump) -- modern
  		// sans-serif + a 4-size type scale. Inter is the primary family
  		// with a system-sans fallback. The fontSize tokens are remapped
  		// so every text-* utility across the app lands on the scale
  		// (body 18, secondary 16, subheading 22, page title 26-28) with
  		// researched line-heights (1.5 body, ~1.3 headings). Raised one
  		// step from the prior 16/14 base -- dense data screens still
  		// read small at 14-16px, especially on high-density displays.
  		fontFamily: {
  			sans: ['Inter', '-apple-system', '"Segoe UI"', 'Roboto', 'system-ui', 'sans-serif'],
  		},
  		fontSize: {
  			xs: ['16px', { lineHeight: '1.5' }],     // secondary / captions / labels / table headers
  			sm: ['18px', { lineHeight: '1.5' }],     // body (collapsed onto base)
  			base: ['18px', { lineHeight: '1.5' }],   // body / default
  			lg: ['22px', { lineHeight: '1.4' }],     // subheading
  			xl: ['22px', { lineHeight: '1.4' }],     // subheading
  			'2xl': ['26px', { lineHeight: '1.3' }],  // page title
  			'3xl': ['28px', { lineHeight: '1.25' }], // page title (large)
  		},
  		borderRadius: {
  			lg: 'var(--radius)',
  			md: 'calc(var(--radius) - 2px)',
  			sm: 'calc(var(--radius) - 4px)'
  		},
  		colors: {
  			background: 'hsl(var(--background))',
  			foreground: 'hsl(var(--foreground))',
  			card: {
  				DEFAULT: 'hsl(var(--card))',
  				foreground: 'hsl(var(--card-foreground))'
  			},
  			popover: {
  				DEFAULT: 'hsl(var(--popover))',
  				foreground: 'hsl(var(--popover-foreground))'
  			},
  			primary: {
  				DEFAULT: 'hsl(var(--primary))',
  				foreground: 'hsl(var(--primary-foreground))'
  			},
  			secondary: {
  				DEFAULT: 'hsl(var(--secondary))',
  				foreground: 'hsl(var(--secondary-foreground))'
  			},
  			muted: {
  				DEFAULT: 'hsl(var(--muted))',
  				foreground: 'hsl(var(--muted-foreground))'
  			},
  			accent: {
  				DEFAULT: 'hsl(var(--accent))',
  				foreground: 'hsl(var(--accent-foreground))'
  			},
  			destructive: {
  				DEFAULT: 'hsl(var(--destructive))',
  				foreground: 'hsl(var(--destructive-foreground))'
  			},
  			border: 'hsl(var(--border))',
  			input: 'hsl(var(--input))',
  			ring: 'hsl(var(--ring))',
  			chart: {
  				'1': 'hsl(var(--chart-1))',
  				'2': 'hsl(var(--chart-2))',
  				'3': 'hsl(var(--chart-3))',
  				'4': 'hsl(var(--chart-4))',
  				'5': 'hsl(var(--chart-5))'
  			}
  		},
  		keyframes: {
  			'accordion-down': {
  				from: {
  					height: '0'
  				},
  				to: {
  					height: 'var(--radix-accordion-content-height)'
  				}
  			},
  			'accordion-up': {
  				from: {
  					height: 'var(--radix-accordion-content-height)'
  				},
  				to: {
  					height: '0'
  				}
  			}
  		},
  		animation: {
  			'accordion-down': 'accordion-down 0.2s ease-out',
  			'accordion-up': 'accordion-up 0.2s ease-out'
  		}
  	}
  },
  plugins: [require("tailwindcss-animate")],
};