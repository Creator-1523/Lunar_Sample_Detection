import argparse
import json
import math
from pathlib import Path
from matplotlib.colors import to_hex, to_rgba, ListedColormap
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
import yaml
from lib import get_standards_characteristics, load_target_minerals, get_formula, load_images

def get_variable_percent(formula, n, epsilon=.000001):
    """
    When a mineral has components that can have varying amounts of elements,
    simulate n examples of the different percentages. The results should add
    up to 1.

    For example, Olivine has a component:

        {Fe: [0, 1], Mg: [0, 1]}

    This means that it can be all Fe, all Mg, or somewhere inbetween.

    Returns a list of tuples with the element and a numpy array of percenages.

    For example:

    [
        ('Fe', np.array([1, .5, .3, .7, 0, ...])),
        ('Mg', np.array([0, .5, .7, .3, 1, ...]))
    ]
    """
    elements = [
        {'element': e, 'min': m[0], 'max': m[1]}
        for e, m in formula.items()
    ]

    base = sum([e['min'] for e in elements])
    remainder = 1 - base
    element_remainders = [e['max'] - e['min'] for e in elements]

    v = np.hstack([
        np.random.uniform(0, e, (n, 1)) for e in element_remainders
    ])

    while remainder > 0:
        s = v.sum(axis=1, keepdims=True)
        v = (v/s)*remainder
        mask = v < element_remainders
        r = np.clip(v - element_remainders, 0, None)
        v = v - r
        v = v + (mask * (r.sum(axis=1) / mask.sum(axis=1)).reshape(-1, 1))
        if np.abs(remainder - v.sum(axis=1)).mean() < epsilon:
            break

    return [(e['element'], e['min']+v[:, i]) for i, e in enumerate(elements)]

def simulate_mass(formula, n):
    """
    Given a mineral formula, return n simulated examples.

    The formula can either be a string as a chemical formula, or a list of
    strings and dicts. See `target_minerals.yaml` for examples. In the case that
    the formula is just a chemical formula string, all n examples will be the
    same.

    Returns a DataFrame where each row is an example and there are two columns
    for each element in the mineral: element_mass which contains the mass and
    element_percent which has that elements perentage of the whole mass of that
    row.
    """
    if isinstance(formula, dict) and "formula" in formula:
        formula = formula["formula"]
    if not isinstance(formula, list):
        formula = [formula]

    mineral_elements = {}
    def append(element, mass):
        if element in mineral_elements:
            mineral_elements[element] += mass
        else:
            mineral_elements[element] = mass

    for component in formula:
        if isinstance(component, str):
            for element, mass in get_formula(component, format="mass").items():
                append(element, np.ones(n)*mass)
        elif isinstance(component, dict):
            if 'quantity' in component:
                quantity = component['quantity']
                if isinstance(quantity, list):
                    quantity = np.random.randint(quantity[0], quantity[1]+1)
            else:
                quantity = 1

            for molecule, percent in get_variable_percent(component['components'], n):
                for element, mass in get_formula(molecule, format="mass").items():
                    append(element, percent*mass*quantity)
        else:
            raise ValueError(f"{str(component)} is not a recognized format")

    # Calculate mass percents
    df = pd.DataFrame(mineral_elements)
    df.columns = [f"{element}_mass" for element in df.columns]
    df['mass'] = df.sum(axis=1)
    for element in mineral_elements:
        df[f"{element}_percent"] = df[f"{element}_mass"]/df['mass']

    return df

def simulate_mineral(mineral, formula, elements, n=100, noise=10):
    """
    Simulate a mineral's intensities as if it were scanned by the electron
    microprobe. Return a DataFrame where each row is one simulated example
    of that mineral.

    Parameters
    ----------
    mineral: str
        The name of the mineral
    formula: str or list or dict
        The formula for the mineral using the format in `target_minerals.yaml`
    elements: dict
        A dict describing the characteristics of each element in the electron
        microprobe scan. Obtained from lib.get_standards_characteristics
    n: int
        The number of examples to create. (Default 100)
    noise: number
        The amount of noise to add to each element channel. More noise will
        allow the classifier to have more tolerance when classifying minerals
        which contain trace amounts of unexpected elements. (Default 10)
    """
    df = simulate_mass(formula, n)

    # Convert to intensities
    for element in elements:
        e = elements[element]
        df[element] = (
            e['intercept'] + np.clip(
                np.random.normal(scale=e['noise']*noise, size=n),
                0, None
            )
        )

        if f"{element}_percent" in df:
            df[element] += (
                e['coef']*df[f"{element}_percent"] +
                np.random.normal(scale=e['std'], size=n)
            )

        df[element] = np.clip(df[element], 0, None)

    df['mineral'] = mineral
    return df

# def main(standards_dir, meteorite_dir, target_minerals_file, output_dir,
#          title=None, bits=32, mask=None, n=100, unknown_n=None, noise=10,
#          model=None, batch_size=100000, output_prefix=''):
def main(title='Mineral Detection', bits=32, mask=None, n=100, unknown_n=None, noise=10,
             model=None, batch_size=100000, output_prefix=''):
    standards_dir=Path('minerals/standards_32bit/')
    meteorite_dir=Path('minerals/data')
    target_minerals_file=Path('minerals/targets_with_color.yaml')
    output_dir=Path('static/mineral/')
    bits = int(bits)
    n = int(n)
    if unknown_n is None:
        unknown_n = n
    unknown_n = int(unknown_n)
    noise = int(noise)
    batch_size = int(batch_size)

    args = locals()
    meteorite_df, meteorite_shape = load_images(meteorite_dir, bits, mask)

    characteristics = get_standards_characteristics(standards_dir, bits)
    target_minerals = load_target_minerals(target_minerals_file)

    #elements = list(characteristics.keys())
    # Only include elements that are in the meteorite images
    elements = [e for e in characteristics.keys() if e in meteorite_df.columns]
    print(f"Using elements: {elements}")

    mineral_colors = {'Unknown': to_rgba('black')}
    mineral_dfs = []
    for mineral, formula in target_minerals.items():
        df = simulate_mineral(mineral, formula, characteristics, n)
        mineral_dfs.append(df)

        if isinstance(formula, dict) and 'color' in formula:
            mineral_colors[mineral] = to_rgba(formula['color'])
        else:
            mineral_colors[mineral] = None

    df = pd.concat(mineral_dfs)
    mineral_mins = df.groupby("mineral").min()[[e for e in elements if e in df.columns]].reset_index()
    df = df[elements + ['mineral']]

    # Assign colors to the minerals that didn't have any specified
    norm = plt.Normalize(0, len(mineral_colors)-1)
    cmap = plt.cm.get_cmap('jet')
    for i, (k) in enumerate(sorted(mineral_colors.keys())):
        if mineral_colors[k] is None:
            mineral_colors[k] = cmap(norm(i))

    if unknown_n > 0:
        unknown = pd.DataFrame(np.clip(
            np.hstack([
                np.random.uniform(-m, m, (unknown_n, 1)) +
                np.random.normal(scale=noise, size=(unknown_n, 1))
                for m in df[elements].max(axis=0)
            ]), 0, None), columns=elements
        )
        unknown['mineral'] = 'Unknown'
        df = pd.concat([df, unknown])


    X = df[elements].values
    Y = df['mineral']
    X_train, X_test, Y_train, Y_test = train_test_split(X, Y, test_size=.2)

    print("Training Classifier...")
    if model is None:
        from sklearn.naive_bayes import GaussianNB
        model = GaussianNB()

    model.fit(X_train, Y_train)

    print("Training Accuracy:", (model.predict(X_train) == Y_train).mean())
    print("Testing Accuracy:", (model.predict(X_test) == Y_test).mean())


    x = meteorite_df[elements].values

    meteorite_df['mineral'] = np.concatenate(list(map(
        model.predict, np.array_split(x, int(math.ceil(len(x) / batch_size)))
    )))

    # Sanity check - remove any classifications where the pixel is missing required elements.
    for i, row in mineral_mins.iterrows():
        filter = meteorite_df['mineral'] == row['mineral']
        #print(elements, row)
        for element in elements:
            if row[element] > 0:
                filter = filter & (meteorite_df[element] == 0)

        meteorite_df.loc[filter, 'mineral'] = "Unknown"


    minerals = sorted(meteorite_df['mineral'].unique())
    if mask:
        masked_minerals = sorted(meteorite_df[meteorite_df['mask'] > 0]['mineral'].unique())
        outputs = ['', '_masked']
    else:
        outputs = ['']

    results = meteorite_df.merge(
        pd.Series(
            minerals, name='mineral'
        ).reset_index().rename(columns={'index': 'mineral_index'}),
        on='mineral'
    ).sort_values('order')

    output_dir = Path(output_dir)
    if not output_dir.exists():
        output_dir.mkdir(parents=True)

    norm = plt.Normalize(0, len(minerals)-1)
    cmap = ListedColormap([mineral_colors[m] for m in minerals])

    color_legend = {m: to_hex(cmap(norm(i))) for i, m in enumerate(minerals)}
    print(color_legend)
    with open(output_dir / f"color_legend.yaml", 'w') as f:
        yaml.dump(color_legend, f, default_flow_style=False)


    for suffix in outputs:
        figure, ax = plt.subplots(figsize=(20,20))

        rgb = np.round(cmap(norm(results['mineral_index'].values.reshape(meteorite_shape)))*255).astype(np.ubyte)
        if suffix:
            rgb[..., -1] = (results['mask'] > 0).values.reshape(meteorite_shape)
        im = ax.imshow(rgb)

        # Save the raw image in the original dimensions
        plt.imsave(output_dir / (f"{output_prefix}figure{suffix}.tiff"), rgb)

        colors = [cmap(norm(i)) for i in range(len(minerals))]
        patches = [
            mpatches.Patch(
                color=colors[i], label=minerals[i]
            ) for i in range(len(minerals))
            if (not suffix) or (minerals[i] in masked_minerals)
        ]
        ax.legend(
            handles=patches, bbox_to_anchor=(1.05, .5),
            borderaxespad=0., fontsize=30, loc="center left"
        )

        if title:
            figure.suptitle(title, fontsize=30, y=.91)

        plt.savefig(
            output_dir / (f"{output_prefix}figure{suffix}.png"),
            facecolor='white', transparent=True,  bbox_inches='tight'
        )

        plt.close()

    def summarize(df, filename, mask):
        path = output_dir / (output_prefix + filename)
        mineral_counts = df.merge(
            pd.Series(list(target_minerals.keys()), name='mineral').to_frame(),
            on='mineral', how='outer'
        ).groupby('mineral').count()['mineral_index'].sort_values(
            ascending=False
        )
        mineral_counts.to_csv(path)
        #print(mineral_counts)

        summary = mineral_counts.to_frame().T

        #summary.columns = summary.iloc[0]
        #summary = summary.iloc[1:]
        summary['path'] = str(path)
        summary['mask'] = mask
        #print(summary)
        return summary

    '''mineral_counts = results.merge(
        pd.Series(list(target_minerals.keys()), name='mineral').to_frame(),
        on='mineral', how='outer'
    ).groupby('mineral').count()['mineral_index'].sort_values(
        ascending=False
    )
    mineral_counts.to_csv(output_dir / 'mineral_counts.csv')'''

    summary = [summarize(results, 'mineral_counts.csv', False)]


    if mask:
        summary.append(summarize(
            results[results['mask'] > 0], 'mineral_counts_masked.csv', True
        ))
        '''results[results['mask'] > 0].merge(
            pd.Series(list(target_minerals.keys()), name='mineral').to_frame(),
            on='mineral', how='outer'
        ).groupby('mineral').count()[
            'mineral_index'
        ].sort_values(ascending=False).to_csv(output_dir / 'mineral_counts_masked.csv')'''

    with open(output_dir / f"{output_prefix}parameters.yaml", 'w') as f:
        yaml.dump(args, f)

    return pd.concat(summary, sort=True)

# Helper function to detect valid directories and files
def valid_path(path_str):
    p = Path(path_str)
    if not p.exists():
        raise argparse.ArgumentTypeError(f"Could not find path {path_str}")
    return p

def valid_dir(path_str):
    p = valid_path(path_str)
    if not p.is_dir():
        raise argparse.ArgumentTypeError(f"Path {path_str} is not a directory")
    return p

def valid_file(path_str):
    p = valid_path(path_str)
    if p.is_dir():
        raise argparse.ArgumentTypeError(f"Path {path_str} is not a file")
    return p

def valid_model(model):
    from sklearn.gaussian_process import GaussianProcessClassifier
    from sklearn.neighbors import KNeighborsClassifier
    from sklearn.naive_bayes import GaussianNB
    from sklearn.svm import SVC
    from sklearn.ensemble import (
        RandomForestClassifier, BaggingClassifier, AdaBoostClassifier
    )
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.neural_network import MLPClassifier

    if (model is None) or (model == "GaussianNB"):
        return GaussianNB()
    elif model == "RandomForest":
        return RandomForestClassifier(50, max_depth=10)
    else:
        return eval(model)

if __name__ == "__main__":
    main()