import copy

import numpy as np
from torch_geometric.data import InMemoryDataset, Data
from torch_geometric.datasets import MD17
import torch_geometric.transforms as T
from tqdm import tqdm


def normalize_md17_energy(dataset, train_indices):
    mean_energy = 0.0
    for idx in train_indices:
        item = dataset[idx]
        mean_energy += item.energy
    mean_energy /= len(train_indices)
    return mean_energy

class WrappedMD17(InMemoryDataset):
    def __init__(self, root: str, name: str, train: bool = True, transform=None, pre_transform=None, pre_filter=None, radius: float = 10.0, num_neighbors: int = 32):
        """
        Initializes the custom dataset.

        Args:
            root (str): Root directory where the dataset should be saved.
            name (str): The name of the molecule to load from the MD17 dataset (e.g., 'Aspirin').
            train (bool, optional): If True, loads the training dataset, otherwise the test dataset. Defaults to True.
            transform (callable, optional): A function/transform that takes in an
                `torch_geometric.data.Data` object and returns a transformed
                version. The data object will be transformed before every access.
                Defaults to None.
            pre_transform (callable, optional): A function/transform that takes in
                an `torch_geometric.data.Data` object and returns a
                transformed version. The data object will be transformed before
                being saved to disk. Defaults to None.
            pre_filter (callable, optional): A function that takes in an
                `torch_geometric.data.Data` object and returns a boolean
                value, indicating whether the data object should be included in the
                final dataset. Defaults to None.
        """
        super().__init__(root, transform, pre_transform, pre_filter)

        self.name = name
        self.train = train
        # Load the original MD17 dataset
        self.md17_dataset = MD17(root=root, name=name)
        # self.compute_edge_indices = T.RadiusGraph(r=radius, max_num_neighbors=num_neighbors)
        self.compute_edge_indices_norm = T.Compose([T.RadiusGraph(r=radius, max_num_neighbors=num_neighbors), T.Distance(norm=False)])
        self.splits = None

        splits = self.get_idx_split()
        train_indices = splits['train'] if 'train' in splits else list(range(self.len()))
        self.mean_energy = normalize_md17_energy(self.md17_dataset, train_indices=train_indices)
        self.splits = splits

    @property
    def raw_file_names(self):
        """A list of files in the raw_dir which needs to be downloaded."""
        # This dataset is built on top of MD17, which handles its own raw files.
        return self.md17_dataset.raw_file_names

    @property
    def processed_file_names(self):
        """A list of files in the processed_dir which are looked for."""
        # We can define our own processed file names if we were to save the
        # encapsulated data. For this example, we process on-the-fly.
        return [f'encapsulated_{self.name}_{"train" if self.train else "test"}.pt']


    def len(self):
        return 1000#len(self.md17_dataset)

    def get(self, idx):
        if self._data:
            return super().get(idx)
        
        if not hasattr(self, '_data_list') or self._data_list is None:
            self._data_list = self.len() * [None]
        elif self._data_list[idx] is not None:
            return copy.copy(self._data_list[idx])
        # Get the original data object from the MD17 dataset
        md17_data = self.md17_dataset[idx]
        md17_data = self.compute_edge_indices_norm(md17_data)

        # pos = md17_data.pos
        # row, col = md17_data.edge_index
        # edge_weight = (pos[row] - pos[col]).norm(dim=-1)

        normalized_energy = md17_data.energy - self.mean_energy

        encapsulated_data = Data(
            x=md17_data.z.unsqueeze(1),
            pos=md17_data.pos,
            edge_index=md17_data.edge_index,
            edge_attr=md17_data.edge_attr,
            edge_weight=md17_data.edge_attr.view(-1),
            y=normalized_energy.squeeze(),
            z=md17_data.z,
        )

        return encapsulated_data
    
    def get_idx_split(self):
        """ Get dataset splits.

        Returns:
            Dict with 'train', 'val', 'test', splits indices.
        """
        if self.splits is not None:
            return self.splits
        
        all_indices = list(range(self.len()))
        # Shuffle the indices using np
        np.random.shuffle(all_indices)

        num_train = int(0.8 * len(all_indices))
        num_val = int(0.1 * len(all_indices))
        num_test = len(all_indices) - num_train - num_val

        self.splits = {
            'train': all_indices[:num_train],
            'val': all_indices[num_train:num_train + num_val],
            'test': all_indices[num_train + num_val:]
        }

        return self.splits
