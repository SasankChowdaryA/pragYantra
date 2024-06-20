import os
from typing import List
import base64
import pickle
import h5py
from transformers import AutoModel, AutoTokenizer
import torch
from sentence_transformers import SentenceTransformer, util
from .summarize import Summarizer
from . import MODELS_DIR, MEMORY_STREAM_DIR
from enum import Enum, auto
import torch.nn.functional as F

class ModelSource(Enum):
    HUGGINGFACE = auto()
    SBERT = auto()

class VectorNode:
    """
    Base representation of a vector node.
    """
    def __init__(self, key: str | int, embedding: torch.Tensor):
        self.key = key
        self.embedding = embedding

    def to_dict(self):
        """
        Returns a dictionary representation of the Node object.
        """
        data = {
            'key': self.key,
            'embedding': self.embedding.tolist()
        }
        return data
    
    @classmethod
    def from_dict(cls, data):
        """
        Creates a Node object from a dictionary.
        """
        tensor_embedding = torch.tensor(data['embedding'], dtype=torch.float32)
        return cls(data['key'], tensor_embedding)
    
    
class VectorEmbeddingModel:
    def __init__(self, model_identifier="all-MiniLM-L6-v2", model_source=ModelSource.SBERT):
        self.model_identifier = model_identifier
        self.model_source = model_source
        self.model = self.load_model()

    def load_model(self):

        if self.model_source == ModelSource.HUGGINGFACE:
            try:
                model_dir = os.path.join(MODELS_DIR,self.model_identifier)
                tokenizer = AutoTokenizer.from_pretrained(model_dir)
                model = AutoModel.from_pretrained(model_dir)
            except:
                tokenizer = AutoTokenizer.from_pretrained(self.model_identifier)
                model = AutoModel.from_pretrained(self.model_identifier)
            return (model, tokenizer)
        
        elif self.model_source == ModelSource.SBERT:
            try:
                path = os.path.join(MODELS_DIR,model)
                return SentenceTransformer(path)
            except:
                return SentenceTransformer(self.model_identifier)
        else:
            # Load non-Hugging Face models here
            raise NotImplementedError("Loading models of this type is not yet implemented yet. You can implement it here.")

    def compute_sentence_embeddings(self, text, **kwargs):
        """
        Compute embeddings for a given text.
        """
        if self.model_source == ModelSource.HUGGINGFACE:
            model, tokenizer = self.model
            inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True, **kwargs)
            with torch.no_grad():
                outputs = model(**inputs)
            # Extract embeddings (e.g., last hidden state)
            embeddings = outputs.last_hidden_state.mean(dim=1)[0]
            return embeddings.numpy()
        
        elif self.model_source == ModelSource.SBERT:
            return self.model.encode(text,convert_to_tensor=True, **kwargs)
        
        else:
            # Compute embeddings for non-Hugging Face models
            raise NotImplementedError("Computing embeddings for this model is not yet implemented yet. You can implement it here.")


    def semantic_search(self,query_embedding, vector_nodes:List[VectorNode], top_k=5,**kwargs):
        """
        Perform a semantic search.

        Args:
            query_embedding (torch.Tensor): The query embedding.
            vector_nodes (List[VectorNode]): The list of VectorNode objects.
            top_k (int, optional): The number of results to return. Defaults to 5.

        Returns:
            List[Tuple[VectorNode, float]]: The list of VectorNode objects and their cosine similarity scores.
        """
        if self.model_source == ModelSource.HUGGINGFACE:
            similarities = [(node, self.cosine_similarity(query_embedding, node.embedding)) for node in vector_nodes]
            similarities.sort(key=lambda x: x[1], reverse=True)
            return similarities[:top_k]
        
        elif self.model_source == ModelSource.SBERT:
            search_results = util.semantic_search(query_embedding, torch.stack([node.embedding for node in vector_nodes]), top_k=top_k, **kwargs)
                # Map the search results to VectorNode objects and their scores
            node_scores = [(vector_nodes[result['corpus_id']], result['score']) for result in search_results[0]]
            
            return node_scores
        
        else:
            # Perform semantic search for non-Hugging Face models
            raise NotImplementedError("Performing semantic search for this model is not yet implemented yet. You can implement it here.")

    @staticmethod
    def cosine_similarity(vector1, vector2):
        """
        Computes the cosine similarity between two vectors.
        Adjusted to handle 1D and 2D tensors for both vector1 and vector2.
        """
        dot_product = torch.dot(torch.tensor(vector1), torch.tensor(vector2))
        norm1 = torch.norm(torch.tensor(vector1))
        norm2 = torch.norm(torch.tensor(vector2))
        return dot_product / (norm1 * norm2)
    
class VectorStore:
    """
    An implementation of Vector Database. A base class for a vector store that manages a collection of vector nodes.
    """
    
    def __init__(self,node_type):
        self.node_type = node_type
        self.summarizer = Summarizer()

    def __str__(self):
        return f"{self.__class__.__name__} with {len(self.vector_nodes)} nodes"
    
    def __add__(self, other):
        self.integrate_databases(other)
        return self
    
    def __len__(self):
        return len(self.vector_nodes)

    @classmethod
    def set_node_type(cls, node_type):
        cls.node_type = node_type

    def save(self, filename):
        """
        Saves the vectors to a file.

        Args:
            filename (str): The name of the file to save the vectors to.
        """
        with h5py.File(os.path.join(MEMORY_STREAM_DIR,filename), 'w') as f:
            for key, vector_node in self.vector_nodes.items():
                # Serialize the value to a byte string and encode it to base64 (to avoid NULL errors) )
                serialized_value = base64.b64encode(pickle.dumps(vector_node.to_dict()))
                f.create_dataset(key, data=serialized_value)

    def load(self, filename):
        """
        Loads the vectors from a file.

        Args:
            filename (str): The name of the file to load the vectors from.
        """
        with h5py.File(os.path.join(MEMORY_STREAM_DIR,filename), 'r') as f:
            for key in f.keys():
                # Decode the value from base64 and deserialize it from a byte string
                data = pickle.loads(base64.b64decode(f[key][()]))
                self.vector_nodes[key] = self.node_type.from_dict(data)
    
    def integrate_databases(self, source_db):
        """
        Integrates the data from the source database into the target database.

        Args:
            source_db (VectorDatabase): The source database.
        """
        if not isinstance(source_db, type(self)):
            raise TypeError("Source database must be of the same type as the target database")

        if self is source_db:
            raise ValueError("Cannot integrate a database with itself")

        for key, node in source_db.vector_nodes.items():
            self.vector_nodes[key] = node
    

    def preprocess_text(self, text, threshold_length=200):
        """
        Preprocesses the input text if its too long by summarizing it.

        Args:
            text (str): The input text to preprocess.
            threshold_length (int, optional): The threshold length for summarizing the text. Defaults to 100.
        Returns:
            str: The preprocessed text.
        """
        if len(text)>threshold_length:
            text = self.summarizer.summarize(text)
        return text

    def vectorize_text(self, text):
        embedding = self.embedding_model.compute_sentence_embeddings(text)
        return embedding
    
    def add_vector(self):
        """
        Adds a vector to the database.
        """
        raise NotImplementedError("VectorDatabase.add_vector method is not implemented")
    
    def query(self, text, date=None, date_type='day', k=5, days_threshold=2):
        raise NotImplementedError("VectorDatabase.query method is not implemented")
    