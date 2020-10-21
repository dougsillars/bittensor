import bittensor

import torch
from torch import nn
import torch.nn.functional as F
import transformers
from transformers import GPT2Config, GPT2Model
from typing import List, Tuple, Dict, Optional


class GPT2LMConfig:
    r"""
    This is the configuration class to store the configuration of a :class:`~GPT2LMSynapse`.
    

    Args:
        huggingface_config (:obj:`transformers.GPT2Config`, `required`, defaults to GPT2LMConfig.__default_huggingface_config__):
            The number of logit heads used by the target layer.      

    examples:

        >>> from bittensor.synapses.ffnn.model import GPT2LMConfig, GPT2LMSynapse

        >>> # Initializing a GPTMLM configuration.
        >>> configuration = GPT2LMConfig()

        >>> # Initializing the model from configuration.
        >>> configuration = GPT2LMSynapse ( configuration )
    """

    __default_huggingface_config__ = GPT2Config(    vocab_size=bittensor.__vocab_size__, 
                                                    n_embd=bittensor.__network_dim__,
                                                    n_layer=3,
                                                    n_head=2, 
                                                    n_inner=None, 
                                                    activation_function='gelu_new', 
                                                    resid_pdrop=0.1, 
                                                    embd_pdrop=0.1, 
                                                    attn_pdrop=0.1, 
                                                    layer_norm_epsilon=1e-05, 
                                                    initializer_range=0.02, 
                                                    summary_type='cls_index', 
                                                    summary_use_proj=True, 
                                                    summary_activation=None, 
                                                    summary_proj_to_labels=True, 
                                                    summary_first_dropout=0.1, 
                                                    bos_token_id=50256, 
                                                    eos_token_id=50256
                                                )
    
    def __init__(self, **kwargs):
        self.huggingface_config = kwargs.pop("huggingface_config", self.__default_huggingface_config__)
        self.run_type_checks()
    
    def run_type_checks(self):
        assert isinstance(self.huggingface_config, transformers.GPT2Config)a
        assert self.huggingface_config.n_embd == bittensor.__network_dim__, "GPT embedding dim {} != {}".format(self.huggingface_config.n_embd, bittensor.__network_dim__)
        assert self.huggingface_config.vocab_size == bittensor.__vocab_size__, "GPT vocab size must match bittensor.__vocab_size {} != {}".format(self.huggingface_config.vocab_size, bittensor.__vocab_size__)

class GPT2Pooler(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.n_embd, config.n_embd)
        self.activation = nn.Tanh()

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class GPT2LMSynapse(bittensor.Synapse):
    """ A Bittensor Synapse training GPT2 with Masked Language Modelling (MLM)
    """

    def __init__(self,
                 config: GPT2LMConfig = None,
                 dendrite: bittensor.Dendrite = None,
                 metagraph: bittensor.Metagraph = None):
        r""" Init a new ffnn synapse module.

            Args:
                config (:obj:`GPT2LMConfig`, `optional`, defaults to GPT2LMConfig()): 
                    GPTMLM configuration class.

                dendrite (:obj:`bittensor.Dendrite`, `optional`, bittensor.dendrite): 
                    bittensor dendrite object used for queries to remote synapses.
                    Defaults to bittensor.dendrite global.

                metagraph (:obj:`bittensor.Metagraph`, `optional`, bittensor.metagraph): 
                    bittensor metagraph containing network graph information. 
                    Defaults to bittensor.metagraph global.

        """
        super(GPT2LMSynapse, self).__init__()
        
        # Bittensor dendrite object used for queries to remote synapses.
        # Defaults to bittensor.dendrite global object.
        self.dendrite = dendrite
        if self.dendrite == None:
            self.dendrite = bittensor.dendrite

        # Bttensor metagraph containing network graph information.
        # Defaults to bittensor.metagraph global object.
        self.metagraph = metagraph
        if self.metagraph == None:
            self.metagraph = bittensor.metagraph

        # Set config or use default huggingface config class.
        self.config = config
        if self.config == None:
            self.config = GPT2LMConfig()

        # encoder_layer: encodes tokenized sequences to embedding size.
        # [batch_size, sequence_len] -> [batch_size, sequence_len, bittensor.__network_dim__]
        self.encoder_transformer = GPT2Model(self.config.huggingface_config)

        # pooler_layer: pools transformed sequence to singe embedding.
        # [batch_size, bittensor.__network_dim__, sequence_len] -> [batch_size, bittensor.__network_dim__]
        self.pooler = GPT2Pooler(self.config.huggingface_config)

        # router: (PKM layer) queries network using initial transform as context.
        # [batch_size, bittensor.__network_dim__] -> topk * [batch_size, bittensor.__network_dim__]
        self.router = bittensor.Router(x_dim=bittensor.__network_dim__, key_dim=100, topk=10)

        # context_transformer: distills the remote_context from inputs
        # [batch_size, sequence_len] -> [batch_size, sequence_len, bittensor.__network_dim__]
        self.context_transformer = GPT2Model(self.config.huggingface_config)

        # hidden_layer: distills the remote_context from inputs
        # [batch_size, sequence_dim, 2 * bittensor.__network_dim__] -> [batch_size, sequence_len, bittensor.__network_dim__]
        self.hidden_layer = torch.nn.Linear(2 * bittensor.__network_dim__, bittensor.__network_dim__)

        # target_layer: maps from hidden layer to vocab dimension for each token. Used of MLM loss.
        # [batch_size, sequence_len, bittensor.__network_dim__] -> [batch_size, sequence_len, bittensor.__vocab_size__]
        self.target_layer = nn.Linear(bittensor.__network_dim__, bittensor.__vocab_size__, bias=False)
        
        # Loss function: MLM cross-entropy loss.
        # predicted: [batch_size, sequence_len, 1], targets: [batch_size, sequence_len, 1] -> [1]
        self.loss_fct = torch.nn.CrossEntropyLoss()

    def forward_text(self, inputs: torch.LongTensor):
        """ Local forward inputs through the MLM GPT Synapse.

            Args:
                inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`): 
                    Batch_size length list of tokenized sentences.
            
            Returns:
                hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`): 
                    Hidden layer representation produced using the local_context.
        """
        hidden = self.forward(inputs=inputs.to(self.device), training = False, remote = False)['local_hidden']
        return hidden

    def forward(self, 
                inputs: torch.LongTensor, 
                training: bool = True, 
                remote: bool = False):
        r""" Forward pass inputs and labels through the GPT MLM module.

            Args:
                inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`): 
                    Batch_size length list of text sentences.

                training (:obj:`bool')`, `optional`, defaults to True):
                    Switch to True if this forward pass computes an MLM loss.

                remote (:obj:`bool')`, `optional`):
                    Switch to True if this forward pass makes a remote call to the network. 

            dictionary with { 
                    loss  (:obj:`List[str]` of shape :obj:`(batch_size)`, `required`):
                        Total loss acumulation to be used by loss.backward()

                    local_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`):
                        Hidden layer encoding produced using student_context.

                    local_target (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__vocab_size__)`, `optional`):
                        GPT MLM Target predictions using student_context. 

                    local_target_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`): 
                        GPT MLM loss using student_context.

                    remote_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `optional`): 
                        Hidden layer encoding produced using the remote_context.

                    remote_target (:obj:`torch.FloatTensor` of shape :obj:`(batch_size,  bittensor.__vocab_size__)`, `optional`):
                        GPT MLM Target predictions using the remote_context.

                    remote_target_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`):
                        GPT MLM loss using the remote_context.

                    distillation_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`): 
                        Distillation loss between student_context and remote_context.
                }
        """

        # Return vars.
        loss = torch.tensor(0.0)
        local_output = None
        network_output = None
        network_target_loss = None
        local_target_loss = None
        distillation_loss = None

        # encoding: encoded sentences into network_dim.
        # encoding.last_hidden_state.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        encoding = self.encoder_transformer(input_ids=inputs, return_dict=True).last_hidden_state
        
        # pooled: pooled encodings,
        # pooled.shape = [batch_size, bittensor.__network_dim__]
        pooled = self.pooler(encoding)

        # remote_context: responses from a bittensor remote network call.
        # remote_context.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        if query:
            # network = torch.Tensor(batch_size, bittensor.__network_dim__)
            synapses = bittensor.metagraph.synapses()  # Returns a list of synapses on the network.
            requests, _ = self.router.route(synapses, pooled, inputs)  # routes inputs to network.
            responses = bittensor.dendrite.forward_text(synapses, requests)  # Makes network calls.
            remote_context = self.router.join(responses)  # Joins responses based on scores..

        # local_context: distillation model for remote_context.
        # local_context.last_hidden_state.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        local_context = self.context_transformer(input_ids=inputs, return_dict=True).last_hidden_state
        if remote:
            # distillation_loss: distillation loss between local_context and remote_context
            # distillation_loss.shape = [1]
            distillation_loss = F.mse_loss(local_context, remote_context.detach())
            loss = loss + distillation_loss

        # local_hidden: hidden layer encoding using local_context.
        # local_hidden.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        local_hidden = torch.cat([encoding, local_context], dim=2)
        local_hidden = self.hidden_layer(local_hidden)
        if training:
            # local_target: projection of local_hidden onto target dimension.
            # local_target.shape = [batch_size, sequence_len, bittensor.__vocab_size__]
            local_target = self.target_layer(local_hidden)

            # local_target_loss: MLM loss between local_target and passed targets.
            # local_target_loss.shape = [1]
            shift_logits = local_target[..., :-1, :].contiguous()
            shift_labels = inputs[..., 1:].contiguous()
            local_target_loss = self.loss_fct(
                shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
            loss = loss + local_target_loss


        if remote:
            # remote_hidden: hidden layer encoding using remote_context.
            # remote_hidden.shape = [batch_size, sequence_len, bittensor.__network_dim__]
            remote_hidden = torch.cat([encoding, remote_context], dim=2)
            remote_hidden = self.hidden_layer(remote_hidden)

            if training:
                # remote_target: projection of remote_hidden onto target dimension.
                # remote_target.shape = [batch_size, sequence_len, bittensor.__vocab_size__]
                remote_target = self.target_layer(local_hidden)

                # remote_target_loss: MLM loss between remote_target and passed targets.
                # remote_target_loss.shape = [1]
                shift_logits = remote_target[..., :-1, :].contiguous()
                shift_labels = inputs[..., 1:].contiguous()
                remote_target_loss = self.loss_fct(
                    shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
                loss = loss + remote_target_loss

        return {
            'loss': loss,
            'local_hidden': local_hidden,
            'local_target': local_target,
            'local_target_loss': local_target_loss,
            'remote_hidden': remote_hidden,
            'remote_target': remote_target,
            'remote_target_loss': remote_target_loss,
            'distillation_loss': distillation_loss,
        }
